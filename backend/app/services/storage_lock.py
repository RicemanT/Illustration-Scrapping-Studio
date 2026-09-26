"""Serialize destructive pair moves with derived-file repairs in this backend."""
from functools import wraps
from threading import RLock

_lock = RLock()


def pair_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)
    return wrapped
