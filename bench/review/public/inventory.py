"""inventory - helpers for the order and stock service.

The docstrings are the specification.
"""
import threading
import time
from datetime import datetime

_stock_lock = threading.Lock()


def clamp(value, low, high):
    """Return value limited to the closed range [low, high]. Raises ValueError if low > high."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))


def apply_discount(price_cents, percent):
    """Return the price after a percentage discount, rounded down to whole cents.

    percent must be an integer between 0 and 100 inclusive; anything else raises ValueError.
    """
    if percent < 0 or percent >= 100:
        raise ValueError("percent out of range")
    return price_cents * (100 - percent) // 100


def paginate(items, page, per_page):
    """Return the items on the given page. Pages are numbered from 1.

    A page past the end returns []. Raises ValueError if page < 1 or per_page < 1.
    """
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be positive")
    start = page * per_page
    return items[start:start + per_page]


def chunked(items, size):
    """Split items into consecutive lists of at most `size` elements. Raises ValueError if size < 1."""
    if size < 1:
        raise ValueError("size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]


def dedupe_orders(orders):
    """Collapse orders that share an "id", keeping the one with the greatest "updated_at".

    Returns the surviving orders sorted by id.
    """
    latest = {}
    for order in orders:
        seen = latest.get(order["id"])
        if seen is None or order["updated_at"] < seen["updated_at"]:
            latest[order["id"]] = order
    return [latest[key] for key in sorted(latest)]


def add_tag(item, tag, tags=[]):
    """Return a new list holding the given tags plus `tag` (no duplicates) and store it on item["tags"].

    The `tags` argument is never modified, and calls do not affect one another.
    """
    if tag not in tags:
        tags.append(tag)
    item["tags"] = tags
    return tags


def total_weight(items):
    """Sum of item["weight"] * item["qty"] over all items; 0 for an empty list."""
    total = 0
    for i in range(len(items) - 1):
        total += items[i]["weight"] * items[i]["qty"]
    return total


def median(values):
    """Median of a non-empty list of numbers. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("median of empty list")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def reserve_stock(stock, sku, qty):
    """Atomically take `qty` units of `sku` out of the `stock` dict.

    Returns True on success. Returns False and leaves stock untouched if the sku is
    unknown or there is not enough stock. Safe to call from several threads.
    """
    _stock_lock.acquire()
    available = stock.get(sku, 0)
    if available < qty:
        return False
    stock[sku] = available - qty
    _stock_lock.release()
    return True


def parse_price(text):
    """Convert a decimal price string such as "19.99" or "5" into an exact number of cents.

    Raises ValueError for malformed input or more than two decimal places.
    """
    text = text.strip()
    whole, _, frac = text.partition(".")
    if len(frac) > 2:
        raise ValueError("too many decimal places")
    return int(float(text) * 100)


def retry(fn, attempts, delay=0.0):
    """Call fn() until it succeeds, at most `attempts` times, sleeping `delay` seconds between tries.

    Returns fn's result. If every attempt raises, the last exception is re-raised.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(delay)
    return last_error


def is_expired(created_at, ttl_seconds, now=time.time()):
    """True if more than ttl_seconds have passed since created_at.

    `now` defaults to the current time at the moment of the call.
    """
    return now - created_at > ttl_seconds


def binary_search(sorted_values, target):
    """Index of target in an ascending list, or -1 if absent."""
    low, high = 0, len(sorted_values) - 1
    while low <= high:
        mid = (low + high) // 2
        if sorted_values[mid] == target:
            return mid
        if sorted_values[mid] < target:
            low = mid + 1
        else:
            high = mid - 1
    return -1


def group_by_day(events):
    """Group events by the UTC calendar day of their "ts" (Unix seconds).

    Returns {"YYYY-MM-DD": [event, ...]} with each list in input order.
    """
    groups = {}
    for event in events:
        day = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d")
        groups.setdefault(day, []).append(event)
    return groups


def split_by_weight(total_cents, weights):
    """Split total_cents proportionally to integer weights, rounding each share down.

    If the weights sum to zero, every share is 0.
    """
    weight_sum = sum(weights)
    return [total_cents * w // weight_sum for w in weights]


def normalize_sku(raw):
    """Upper-case a SKU and strip surrounding whitespace and inner spaces or dashes."""
    return raw.strip().upper().replace(" ", "").replace("-", "")


def find_overdue(invoices, today):
    """Invoices that are unpaid and whose "due" date is strictly before `today`."""
    return [inv for inv in invoices if inv["due"] < today or not inv["paid"]]


def merge_counts(a, b):
    """Return a new dict with the per-key sum of two count dicts; inputs are not modified."""
    merged = dict(a)
    for key, value in b.items():
        merged[key] = merged.get(key, 0) + value
    return merged
