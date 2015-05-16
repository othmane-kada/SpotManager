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
from pyLibrary.dot import wrap, unwrap

_get = object.__getattribute__
_set = object.__setattr__
WRAPPED_CLASSES = set()

class DictObject(dict):

    def __init__(self, obj):
        dict.__init__(self)
        _set(self, "_obj", obj)
        try:
            if isinstance(obj, dict):
                _set(self, "_dict", wrap(obj))
            else:
                _set(self, "_dict", wrap(_get(obj, "__dict__")))
        except Exception, _:
            pass

    def __getattr__(self, item):
        try:
            output = _get(_get(self, "_obj"), item)
            if output == None:
                return None   # So we allow `is` compare to `None`
            return wrap(output)
        except Exception, _:
            return wrap(_get(self, "_dict")[item])

    def __setattr__(self, key, value):
        _get(self, "_dict")[key] = value

    def __getitem__(self, item):
        try:
            return wrap(_get(self, "_dict")[item])
        except Exception, e:
            raise e

    def keys(self):
        return _get(self, "_dict").keys()

    def items(self):
        return _get(self, "_dict").items()

    def __iter__(self):
        return _get(self, "_dict").__iter__()

    def __str__(self):
        return _get(self, "_dict").__str__()

    def __len__(self):
        return _get(self, "_dict").__len__()

    def __call__(self, *args, **kwargs):
        return _get(self, "_obj")(*args, **kwargs)


class DictClass(object):
    """
    ALLOW INSTANCES OF class_ TO ACK LIKE dicts
    ALLOW CONSTRUCTOR TO ACCEPT @use_settings
    """

    def __init__(self, class_):
        WRAPPED_CLASSES.add(class_)
        self.class_ = class_
        self.constructor = class_.__init__

    def __call__(self, *args, **kwargs):
        settings = wrap(kwargs).settings

        params = self.constructor.func_code.co_varnames[1:self.constructor.func_code.co_argcount]
        if not self.constructor.func_defaults:
            defaults = {}
        else:
            defaults = {k: v for k, v in zip(reversed(params), reversed(self.constructor.func_defaults))}

        ordered_params = dict(zip(params, args))

        output = self.class_(**params_pack(params, ordered_params, kwargs, settings, defaults))
        return DictObject(output)


def params_pack(params, *args):
    settings = {}
    for a in args:
        for k, v in a.items():
            k = unicode(k)
            if k in settings:
                continue
            settings[k] = v

    output = {str(k): unwrap(settings[k]) for k in params if k in settings}
    return output


