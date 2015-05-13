# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import unicode_literals
from __future__ import division

import boto
from boto.ec2.blockdevicemapping import BlockDeviceType, BlockDeviceMapping
from boto.ec2.networkinterface import NetworkInterfaceSpecification, NetworkInterfaceCollection
from boto.ec2.spotpricehistory import SpotPriceHistory
from boto.utils import ISO8601

from pyLibrary import convert
from pyLibrary.collections import SUM
from pyLibrary.debugs import startup
from pyLibrary.debugs.logs import Log
from pyLibrary.debugs.startup import SingleInstance
from pyLibrary.dot import wrap, dictwrap, coalesce, listwrap, unwrap, DictList, get_attr
from pyLibrary.env.files import File
from pyLibrary.maths import Math
from pyLibrary.meta import use_settings, new_instance
from pyLibrary.queries import qb
from pyLibrary.queries.expressions import CODE
from pyLibrary.queries.unique_index import UniqueIndex
from pyLibrary.thread.threads import Lock, Thread, MAIN_THREAD, Signal, Queue
from pyLibrary.times.dates import Date
from pyLibrary.times.durations import DAY, HOUR, WEEK, MINUTE, SECOND, Duration
from pyLibrary.times.timer import Timer

DEBUG_PRICING = False
TIME_FROM_RUNNING_TO_LOGIN = 5 * MINUTE

class SpotManager(object):
    @use_settings
    def __init__(self, instance_manager, settings):
        self.settings = settings
        self.instance_manager = instance_manager
        aws_args = dict(
            region_name=settings.aws.region,
            aws_access_key_id=unwrap(settings.aws.aws_access_key_id),
            aws_secret_access_key=unwrap(settings.aws.aws_secret_access_key))
        self.ec2_conn = boto.ec2.connect_to_region(**aws_args)
        self.vpc_conn = boto.vpc.connect_to_region(**aws_args)
        self.price_locker = Lock()
        self.prices = None
        self.price_lookup = None
        self.done_spot_requests = Signal()
        self.net_new_locker = Lock()
        self.net_new_spot_requests = UniqueIndex(("id",))
        self.watcher = None
        if instance_manager.setup_required():
            self._start_life_cycle_watcher()
        self.pricing()

    def update_spot_requests(self, utility_required):
        spot_requests = self._get_managed_spot_requests()

        # ADD UP THE CURRENT REQUESTED INSTANCES
        active = qb.filter(spot_requests, {"terms": {"status.code": RUNNING_STATUS_CODES | PENDING_STATUS_CODES}})
        used_budget = 0
        current_spending = 0
        for a in active:
            about = self.price_lookup[a.launch_specification.instance_type]
            Log.note("Active Spot Request {{id}}: {{type}} @ {{price|round(decimal=4)}}", {
                "id": a.id,
                "type": a.launch_specification.instance_type,
                "price": a.price - about.type.discount
            })
            used_budget += a.price - about.type.discount
            current_spending += about.current_price - about.type.discount

        Log.note("TOTAL BUDGET: ${{budget|round(decimal=4)}}/hour (current price: ${{current|round(decimal=4)}}/hour)", {
            "budget": used_budget,
            "current": current_spending
        })

        remaining_budget = self.settings.budget - used_budget

        current_utility = coalesce(SUM(self.price_lookup[r.launch_specification.instance_type].type.utility for r in active), 0)
        net_new_utility = utility_required - current_utility

        if remaining_budget < 0:
            remaining_budget, net_new_utility = self.save_money(remaining_budget, net_new_utility)

        if net_new_utility <= 0:
            net_new_utility = self.remove_instances(net_new_utility)

        if net_new_utility > 0:
            net_new_utility = Math.min(net_new_utility, self.settings.max_new_utility)
            net_new_utility, remaining_budget = self.add_instances(net_new_utility, remaining_budget)

        if net_new_utility > 0:
            Log.alert("Can not fund {{num|round(places=2)}} more utility (all utility costs more than ${{expected|round(decimal=2)}}/hour).  Remaining budget is ${{budget|round(decimal=2)}} ", {
                "num": net_new_utility,
                "expected": self.settings.max_utility_price,
                "budget": remaining_budget
            })

        Log.note("All requests for new utility have been made")
        self.done_spot_requests.go()

    def add_instances(self, net_new_utility, remaining_budget):
        prices = self.pricing()

        for p in prices:
            if p.current_price==None:
                Log.note("{{type}} has no price", {
                    "type": p.type.instance_type
                })
                continue
            max_bid = Math.min(p.higher_price, p.type.utility * self.settings.max_utility_price)
            min_bid = p.price_80

            if min_bid > max_bid:
                Log.note("{{type}} @ {{price|round(decimal=4)}}/hour is over budget of {{limit}}", {
                    "type": p.type.instance_type,
                    "price": min_bid,
                    "limit": p.type.utility * self.settings.max_utility_price
                })
                continue

            num = int(Math.round(net_new_utility / p.type.utility))
            if num == 1:
                min_bid = Math.min(Math.max(p.current_price*1.1, Math.min(min_bid, max_bid)), p.type.utility * self.settings.max_utility_price)
                price_interval = 0
            else:
                price_interval = Math.min(min_bid/10, (max_bid - min_bid) / (num - 1))

            for i in range(num):
                bid = min_bid + (i * price_interval)
                if bid < p.current_price or bid > remaining_budget:
                    continue

                try:
                    new_requests = self._request_spot_instances(
                        price=bid,
                        availability_zone_group=p.availability_zone,
                        instance_type=p.type.instance_type,
                        settings=self.settings.ec2.request
                    )
                    Log.note("Request {{num}} instance {{type}} with utility {{utility}} at ${{price}}/hour", {
                        "num": len(new_requests),
                        "type": p.type.instance_type,
                        "utility": p.type.utility,
                        "price": bid
                    })
                    net_new_utility -= p.type.utility * len(new_requests)
                    remaining_budget -= bid * len(new_requests)
                    with self.net_new_locker:
                        for ii in new_requests:
                            self.net_new_spot_requests.add(dictwrap(ii))
                except Exception, e:
                    Log.note("Request instance {{type}} failed bcause {{reason}}", {
                        "type": p.type.instance_type,
                        "reason": e.message
                    })

        return net_new_utility, remaining_budget

    def remove_instances(self, net_new_utility):
        instances = self.running_instances()

        # FIND COMBO THAT WILL SHUTDOWN WHAT WE NEED EXACTLY, OR MORE
        remove_list = []
        for acceptable_error in range(0, 8):
            remaining_utility = -net_new_utility
            remove_list = DictList()
            for s in instances:
                utility = coalesce(s.markup.type.utility, 0)
                if utility <= remaining_utility + acceptable_error:
                    remove_list.append(s)
                    remaining_utility -= utility
            if remaining_utility <= 0:
                net_new_utility = -remaining_utility
                break

        if not remove_list:
            return net_new_utility

        # SEND SHUTDOWN TO EACH INSTANCE
        Log.note("Shutdown {{instances}}", {"instances": remove_list.id})
        for i in remove_list:
            try:
                self.instance_manager.teardown(i)
            except Exception, e:
                Log.warning("Teardown of {{id}} failed", {"id": i.id}, e)

        remove_spot_requests = remove_list.spot_instance_request_id

        # TERMINATE INSTANCES
        self.ec2_conn.terminate_instances(instance_ids=remove_list.id)

        # TERMINATE SPOT REQUESTS
        self.ec2_conn.cancel_spot_instance_requests(request_ids=remove_spot_requests)

        return net_new_utility

    def running_instances(self):
        # FIND THE BIGGEST, MOST EXPENSIVE REQUESTS
        instances = self._get_managed_instances()
        for r in instances:
            r.markup = self.price_lookup[r.instance_type]
        instances = qb.sort(instances, [
            {"value": "markup.type.utility", "sort": -1},
            {"value": "markup.estimated_value", "sort": 1}
        ])
        return instances

    def save_money(self, remaining_budget, net_new_utility):
        remove_spot_requests = wrap([])

        # IF WE ARE STILL OUT OF MONEY, THEN CANCEL ALL REQUESTS
        if remaining_budget < 0:
            requests = self._get_managed_spot_requests()
            for r in requests:
                remove_spot_requests.append(r.id)

        instances = self.running_instances()

        remove_list = wrap([])
        for s in instances:
            if remaining_budget >= 0:
                break
            remove_list.append(s)
            net_new_utility += coalesce(s.markup.type.utility, 0)
            remaining_budget += coalesce(s.markup.price_80, s.markup.curret_price)

        # SEND SHUTDOWN TO EACH INSTANCE
        Log.note("Shutdown {{instances}}", {"instances": remove_list.id})
        for i in remove_list:
            try:
                self.instance_manager.teardown(i)
            except Exception, e:
                Log.warning("Teardown of {{id}} failed", {"id": i.id}, e)

        remove_spot_requests.extend(remove_list.spot_instance_request_id)

        # TERMINATE INSTANCES
        self.ec2_conn.terminate_instances(instance_ids=remove_list.id)

        # TERMINATE SPOT REQUESTS
        self.ec2_conn.cancel_spot_instance_requests(request_ids=remove_spot_requests)
        return remaining_budget, net_new_utility

    def _get_managed_spot_requests(self):
        output = wrap([dictwrap(r) for r in self.ec2_conn.get_all_spot_instance_requests() if not r.tags.get("Name") or r.tags.get("Name").startswith(self.settings.ec2.instance.name)])
        # Log.note("got spot from amazon {{spot_ids}}", {"spot_ids":output.id})
        return output

    def _get_managed_instances(self):
        output =[]
        reservations = self.ec2_conn.get_all_instances()
        for res in reservations:
            for instance in res.instances:
                if instance.tags.get('Name', '').startswith(self.settings.ec2.instance.name) and instance._state.name == "running":
                    output.append(dictwrap(instance))
        return wrap(output)

    def _start_life_cycle_watcher(self):
        def life_cycle_watcher(please_stop):
            while not please_stop:
                spot_requests = self._get_managed_spot_requests()
                last_get = Date.now()
                instances = wrap({i.id: dictwrap(i) for r in self.ec2_conn.get_all_instances() for i in r.instances})
                # INSTANCES THAT REQUIRE SETUP
                time_to_stop_trying = {}
                please_setup = [(i, r) for i, r in [(instances[r.instance_id], r) for r in spot_requests] if i.id and not i.tags.get("Name") and i._state.name == "running"]
                for i, r in please_setup:
                    try:
                        p = [u for u in self.settings.utility if u.instance_type == i.instance_type][0]
                        i.markup = p
                        self.instance_manager.setup(i, p.utility)
                        i.add_tag("Name", self.settings.ec2.instance.name + " (running)")
                        with self.net_new_locker:
                            self.net_new_spot_requests.remove(r.id)
                    except Exception, e:
                        if not time_to_stop_trying.get(i.id):
                            time_to_stop_trying[i.id] = Date.now() + TIME_FROM_RUNNING_TO_LOGIN
                        if Date.now() > time_to_stop_trying[i.id]:
                            # FAIL TO SETUP AFTER 5 MINUTES, THEN TERMINATE INSTANCE
                            self.ec2_conn.terminate_instances(instance_ids=[i.id])
                            with self.net_new_locker:
                                self.net_new_spot_requests.remove(r.id)
                            Log.warning("Second problem with setup of {{instance_id}}.  Instance TERMINATED!", {"instance_id": i.id}, e)
                        else:
                            Log.warning("Problem with setup of {{instance_id}}", {"instance_id": i.id}, e)

                if Date.now() - last_get > 5 * SECOND:
                    # REFRESH STALE
                    spot_requests = self._get_managed_spot_requests()
                    last_get = Date.now()


                pending = qb.filter(spot_requests, {"terms": {"status.code": PENDING_STATUS_CODES}})
                if self.done_spot_requests:
                    with self.net_new_locker:
                        expired = Date.now() - self.settings.run_interval + 2 * MINUTE
                        for ii in list(self.net_new_spot_requests):
                            if Date(ii.create_time) < expired:
                                ## SOMETIMES REQUESTS NEVER GET INTO THE MAIN LIST OF REQUESTS
                                self.net_new_spot_requests.remove(ii)
                        pending = UniqueIndex(("id",), data=pending)
                        pending = pending | self.net_new_spot_requests

                if not pending and not time_to_stop_trying and self.done_spot_requests:
                    Log.note("No more pending spot requests")
                    please_stop.go()
                    break
                elif pending:
                    Log.note("waiting for spot requests: {{pending}}", {"pending": qb.select(pending, "id")})

                Thread.sleep(seconds=10, please_stop=please_stop)

            Log.note("life cycle watcher has stopped")

        self.watcher = Thread.run("lifecycle watcher", life_cycle_watcher)

    @use_settings
    def _request_spot_instances(self, price, availability_zone_group, instance_type, settings=None):
        settings.network_interfaces = NetworkInterfaceCollection()

        for interface_settings in listwrap(settings.network_interfaces):
            try:
                subnet = self.vpc_conn.get_all_subnets(filters={'subnet_id': interface_settings.subnet_id})[0]

                if subnet.availability_zone == availability_zone_group:
                    settings.network_interfaces.append(NetworkInterfaceSpecification(**unwrap(interface_settings)))
            except IndexError:
                Log.warning("subnet %s not found; skipping" % interface_settings.subnet_id)

        if len(settings.network_interfaces) == 0:
            Log.error("No network interface specifications found for %s!" % availability_zone_group)

        settings.settings = None

        #INCLUDE EPHEMERAL STORAGE BlockDeviceMapping
        settings.block_device_map = BlockDeviceMapping()
        for i in range(ephemeral_storage[instance_type]["num"]):
            letter = convert.ascii2char(98 + i)
            settings.block_device_map["/dev/sd" + letter] = BlockDeviceType(
                ephemeral_name='ephemeral' + unicode(i),
                # size=ephemeral_storage[instance_type]["size"],
                delete_on_termination=True
            )

        if settings.expiration:
            settings.valid_until = (Date.now() + Duration(settings.expiration)).format(ISO8601)
            settings.expiration = None

        output = list(self.ec2_conn.request_spot_instances(**unwrap(settings)))
        for o in output:
            o.add_tag("Name", self.settings.ec2.instance.name)
        return output

    def pricing(self):
        with self.price_locker:
            if self.prices:
                return self.prices

            prices = self._get_spot_prices_from_aws()

            hourly_pricing = qb.run({
                "from": {
                    # AWS PRICING ONLY SENDS timestamp OF CHANGES, MATCH WITH NEXT INSTANCE
                    "from": prices,
                    "window": {
                        "name": "expire",
                        "value": CODE("coalesce(rows[rownum+1].timestamp, Date.eod())"),
                        "edges": ["availability_zone", "instance_type"],
                        "sort": "timestamp"
                    }
                },
                "edges": [
                    "availability_zone",
                    "instance_type",
                    {
                        "name": "time",
                        "range": {"min": "timestamp", "max": "expire", "mode": "inclusive"},
                        "domain": {"type": "time", "min": Date.now().floor(HOUR) - DAY, "max": Date.now().floor(HOUR), "interval": "hour"}
                    }
                ],
                "select": [
                    {"value": "price", "aggregate": "max"},
                    {"aggregate": "count"}
                ],
                "where": {"gt": {"timestamp": Date.now().floor(HOUR) - DAY}},
                "window": {
                    "name": "current_price", "value": CODE("rows.last().price"), "edges": ["availability_zone", "instance_type"], "sort": "time",
                }
            }).data

            bid80 = qb.run({
                "from": hourly_pricing,
                "edges": [
                    {
                        "value": "availability_zone",
                        "allowNulls": False
                    },
                    {
                        "name": "type",
                        "value": "instance_type",
                        "allowNulls": False,
                        "domain": {"type": "set", "key": "instance_type", "partitions": self.settings.utility}
                    }
                ],
                "select": [
                    {"name": "price_80", "value": "price", "aggregate": "percentile", "percentile": self.settings.bid_percentile},
                    {"name": "max_price", "value": "price", "aggregate": "max"},
                    {"aggregate": "count"},
                    {"value": "current_price", "aggregate": "one"},
                    {"name": "all_price", "value": "price", "aggregate": "list"}
                ],
                "window": [
                    {"name": "estimated_value", "value": {"div": ["type.utility", "price_80"]}},
                    {"name": "higher_price", "value": lambda row: find_higher(row.all_price, row.price_80)}
                ]
            })

            output = qb.run({
                "from": bid80.data,
                "sort": {"value": "estimated_value", "sort": -1}
            })

            self.prices = output.data
            self.price_lookup = {p.type.instance_type: p for p in self.prices}
            return self.prices

    def _get_spot_prices_from_aws(self):
        with Timer("Read pricing file"):
            try:
                content = File(self.settings.price_file).read()
                cache = convert.json2value(content, flexible=False, paths=False)
            except Exception, e:
                cache = DictList()

        most_recents = qb.run({
            "from": cache,
            "edges": ["instance_type"],
            "select": {"value": "timestamp", "aggregate": "max"}
        }).data

        prices = set(cache)
        with Timer("Get pricing from AWS"):
            for instance_type in self.settings.utility.instance_type:
                if most_recents:
                    most_recent = most_recents[{"instance_type":instance_type}].timestamp
                    if most_recent == None:
                        start_at = Date.today() - WEEK
                    else:
                        start_at = Date(most_recent)
                else:
                    start_at = Date.today() - WEEK
                if DEBUG_PRICING:
                    Log.note("get pricing for {{instance_type}} starting at {{start_at}}", {
                        "instance_type": instance_type,
                        "start_at": start_at
                    })

                next_token=None
                while True:
                    resultset = self.ec2_conn.get_spot_price_history(
                        product_description="Linux/UNIX (Amazon VPC)",
                        instance_type=instance_type,
                        availability_zone=self.settings.availability_zone,
                        start_time=start_at.format(ISO8601),
                        next_token=next_token
                    )
                    next_token = resultset.next_token

                    for p in resultset:
                        prices.add(wrap({
                            "availability_zone": p.availability_zone,
                            "instance_type": p.instance_type,
                            "price": p.price,
                            "product_description": p.product_description,
                            "region": p.region.name,
                            "timestamp": Date(p.timestamp)
                        }))

                    if not next_token:
                        break

        with Timer("Save prices to (pretty) file"):
            File(self.settings.price_file).write(convert.value2json(prices, pretty=True))
        return prices


def find_higher(candidates, reference):
    """
    RETURN ONE PRICE HIGHER THAN reference
    """
    output = wrap([c for c in candidates if c > reference])[0]
    return output


TERMINATED_STATUS_CODES = {
    "capacity-oversubscribed",
    "capacity-not-available",
    "instance-terminated-capacity-oversubscribed",
    "bad-parameters"
}
RETRY_STATUS_CODES = {
    "instance-terminated-by-price",
    "bad-parameters",
    "canceled-before-fulfillment",
    "instance-terminated-by-user"
}
PENDING_STATUS_CODES = {
    "pending-evaluation",
    "pending-fulfillment",
    "az-group-constraint",
    "price-too-low"
}
RUNNING_STATUS_CODES = {
    "fulfilled",
    "request-canceled-and-instance-running"
}


def main():
    try:
        settings = startup.read_settings()
        settings.run_interval = Duration(settings.run_interval)
        Log.start(settings.debug)
        with SingleInstance(flavor_id=settings.args.filename):
            instance_manager = new_instance(settings.instance)
            for u in settings.utility:
                u.discount = coalesce(u.discount, 0)
            m = SpotManager(instance_manager, settings=settings)
            m.update_spot_requests(instance_manager.required_utility())

            if m.watcher:
                m.watcher.join()
    except Exception, e:
        Log.warning("Problem with spot manager", e)
    finally:
        Log.stop()
        MAIN_THREAD.stop()


ephemeral_storage = {
    "c1.medium": {"num": 1, "size": 350},
    "c1.xlarge": {"num": 4, "size": 420},
    "c3.2xlarge": {"num": 2, "size": 80},
    "c3.4xlarge": {"num": 2, "size": 160},
    "c3.8xlarge": {"num": 2, "size": 320},
    "c3.large": {"num": 2, "size": 16},
    "c3.xlarge": {"num": 2, "size": 40},
    "c4.2xlarge": {"num": 0, "size": 0},
    "c4.4xlarge": {"num": 0, "size": 0},
    "c4.8xlarge": {"num": 0, "size": 0},
    "c4.large": {"num": 0, "size": 0},
    "c4.xlarge": {"num": 0, "size": 0},
    "cc2.8xlarge": {"num": 4, "size": 840},
    "cg1.4xlarge": {"num": 2, "size": 840},
    "cr1.8xlarge": {"num": 2, "size": 120},
    "d2.2xlarge": {"num": 6, "size": 2000},
    "d2.4xlarge": {"num": 12, "size": 2000},
    "d2.8xlarge": {"num": 24, "size": 2000},
    "d2.xlarge": {"num": 3, "size": 2000},
    "g2.2xlarge": {"num": 1, "size": 60},
    "hi1.4xlarge": {"num": 2, "size": 1024},
    "hs1.8xlarge": {"num": 24, "size": 2000},
    "i2.2xlarge": {"num": 2, "size": 800},
    "i2.4xlarge": {"num": 4, "size": 800},
    "i2.8xlarge": {"num": 8, "size": 800},
    "i2.xlarge": {"num": 1, "size": 800},
    "m1.large": {"num": 2, "size": 420},
    "m1.medium": {"num": 1, "size": 410},
    "m1.small": {"num": 1, "size": 160},
    "m1.xlarge": {"num": 4, "size": 420},
    "m2.2xlarge": {"num": 1, "size": 850},
    "m2.4xlarge": {"num": 2, "size": 840},
    "m2.xlarge": {"num": 1, "size": 420},
    "m3.2xlarge": {"num": 2, "size": 80},
    "m3.large": {"num": 1, "size": 32},
    "m3.medium": {"num": 1, "size": 4},
    "m3.xlarge": {"num": 2, "size": 40},
    "r3.2xlarge": {"num": 1, "size": 160},
    "r3.4xlarge": {"num": 1, "size": 320},
    "r3.8xlarge": {"num": 2, "size": 320},
    "r3.large": {"num": 1, "size": 32},
    "r3.xlarge": {"num": 1, "size": 80},
    "t1.micro": {"num": 0, "size": 0},
    "t2.medium": {"num": 0, "size": 0},
    "t2.micro": {"num": 0, "size": 0},
    "t2.small": {"num": 0, "size": 0}
}



if __name__ == "__main__":
    main()
