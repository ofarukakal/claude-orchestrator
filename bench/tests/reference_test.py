"""Reference tests: every mutant in key.json must be killed by this file (bench.py selfcheck)."""
from billing import LRUCache, merge_intervals, moving_average, parse_duration, split_amount


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False


assert merge_intervals([]) == []
assert merge_intervals([(1, 10), (2, 3), (4, 5)]) == [(1, 10)]
assert merge_intervals([(2, 3), (1, 2), (7, 8)]) == [(1, 3), (7, 8)]
assert merge_intervals([(5, 6), (1, 2)]) == [(1, 2), (5, 6)]

assert split_amount(10, 3) == [4, 3, 3]
assert split_amount(-10, 3) == [-3, -3, -4]
big = split_amount(10**17 + 1, 3)
assert sum(big) == 10**17 + 1 and max(big) - min(big) <= 1
assert raises(lambda: split_amount(5, 0)) and raises(lambda: split_amount(5, -1))

assert abs(parse_duration("250ms") - 0.25) < 1e-9
assert parse_duration("2m 10s") == 130.0 and parse_duration("1h30m") == 5400.0
for bad in ["", "  ", "10", "abc", "5x", "1h foo"]:
    assert raises(lambda bad=bad: parse_duration(bad)), bad

c = LRUCache(2)
c.put("a", 1); c.put("b", 2)
assert c.keys() == ["a", "b"]
assert c.get("a") == 1 and c.keys() == ["b", "a"]
c.put("c", 3)
assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
c = LRUCache(2)
c.put("a", 1); c.put("b", 2); c.put("a", 9)
assert c.keys() == ["b", "a"] and c.get("b") == 2 and c.get("a") == 9
c = LRUCache(2)
c.put("a", 1); c.put("b", 2); c.put("b", 7)
assert c.keys() == ["a", "b"] and c.get("a") == 1
c = LRUCache(1)
c.put("a", 1); c.put("b", 2)
assert c.keys() == ["b"]

assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
assert moving_average([1, 2], 3) == []
assert raises(lambda: moving_average([1, 2], 0)) and raises(lambda: moving_average([1, 2], -1))
