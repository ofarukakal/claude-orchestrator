"""billing - small helpers used across the billing service.

The docstrings are the specification.
"""
import re
from collections import OrderedDict


def merge_intervals(intervals):
    """Merge overlapping or touching closed intervals [(start, end), ...].

    Input may be in any order. Returns a sorted list of merged tuples.
    Touching intervals such as (1, 2) and (2, 3) merge into (1, 3).
    """
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def split_amount(total_cents, n):
    """Split an integer amount of cents into exactly n integer parts.

    The parts always sum exactly to total_cents, differ from each other by at
    most 1, and are returned in descending order. Works for any integer total,
    including negative totals (refunds) and very large totals.
    Raises ValueError if n <= 0.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    base, rem = divmod(total_cents, n)
    return [base + 1] * rem + [base] * (n - rem)


_PART = re.compile(r"(\d+)\s*(ms|h|m|s)\s*")
_UNIT = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_duration(text):
    """Parse a duration such as '1h30m', '45s', '2m 10s', '250ms' or '1h' into seconds (float).

    Units: h, m, s, ms. Parts may be separated by spaces.
    Raises ValueError on empty or malformed input (for example '', '10', 'abc', '5x', '1h foo').
    """
    stripped = text.strip()
    if not stripped or not re.fullmatch(r"(?:\d+\s*(?:ms|h|m|s)\s*)+", stripped):
        raise ValueError("malformed duration: %r" % text)
    total = 0.0
    for amount, unit in _PART.findall(stripped):
        total += int(amount) * _UNIT[unit]
    return total


class LRUCache:
    """Least-recently-used cache with a fixed capacity.

    get(key) returns the value, or None if absent, and marks the key as most
    recently used. put(key, value) inserts or updates; updating an existing key
    refreshes its recency and never evicts anything. Inserting a new key into a
    full cache evicts the least recently used key.
    """

    def __init__(self, capacity):
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key):
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        if key not in self._data and len(self._data) >= self.capacity:
            self._data.popitem(last=False)
        self._data[key] = value
        self._data.move_to_end(key)

    def keys(self):
        """Keys from least to most recently used."""
        return list(self._data.keys())


def moving_average(xs, k):
    """Averages of every full window of size k: returns len(xs) - k + 1 values.

    If k > len(xs) returns []. Raises ValueError if k <= 0.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    out = []
    for i in range(len(xs) - k + 1):
        out.append(sum(xs[i:i + k]) / k)
    return out
