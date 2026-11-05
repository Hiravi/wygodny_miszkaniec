"""LRU response storage bounded by entries and serialized key/value bytes."""

import json
import threading
from collections import OrderedDict
from functools import wraps


class ResponseCache(OrderedDict):
    def __init__(self, max_bytes=4 * 1024 * 1024, max_entries=64):
        super().__init__()
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.bytes = 0
        self.sizes = {}

    def __setitem__(self, key, value):
        def encode(item):
            return item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item)
        size = (
            len(value)
            if isinstance(value, bytes)
            else len(
                json.dumps(
                    value,
                    default=encode,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        )
        size += len(json.dumps(key, default=encode).encode("utf-8"))
        if key in self:
            self.bytes -= self.sizes.pop(key)
            super().__delitem__(key)
        if size > self.max_bytes:
            return
        super().__setitem__(key, value)
        self.sizes[key] = size
        self.bytes += size
        self.move_to_end(key)
        while self.bytes > self.max_bytes or len(self) > self.max_entries:
            self.popitem(last=False)

    def popitem(self, last=True):
        key, value = super().popitem(last=last)
        self.bytes -= self.sizes.pop(key)
        return key, value

    def clear(self):
        super().clear()
        self.sizes.clear()
        self.bytes = 0


def response_cache(max_bytes=8 * 1024 * 1024, max_entries=16):
    """Bound encoded spatial responses; source signatures remain in call keys."""

    def decorate(function):
        cache = ResponseCache(max_bytes, max_entries)
        lock = threading.RLock()
        missing = object()

        @wraps(function)
        def wrapped(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            with lock:
                value = cache.get(key, missing)
                if value is not missing:
                    cache.move_to_end(key)
                    return value
            value = function(*args, **kwargs)
            with lock:
                cache[key] = value
            return value

        def clear():
            with lock:
                cache.clear()

        wrapped.cache_clear = clear
        wrapped.response_cache = cache
        return wrapped

    return decorate
