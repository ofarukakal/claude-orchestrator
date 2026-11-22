"""ledger - accounting rules for a members' cooperative fund.

The docstrings are the specification. Money is Decimal, periods are "YYYYMM" strings,
and every datetime is timezone-aware.
"""
import re
from datetime import timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

TURKEY = timezone(timedelta(hours=3))
CENT = Decimal("0.01")
PERMISSIONS = {"viewer": set(), "cashier": {"collect"}, "accountant": {"collect", "write"}}


def parse_amount(text):
    """Parse an amount typed in Turkish format into a Decimal with two decimals.

    "." groups thousands and "," is the decimal separator: "1.250" is 1250.00,
    "1.250,5" is 1250.50, "12,5" is 12.50 and "300" is 300.00.
    Raises ValueError for anything else, including empty or negative input.
    """
    cleaned = text.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise ValueError("not an amount: %r" % text)
    return Decimal(cleaned).quantize(CENT, rounding=ROUND_HALF_UP)


def valid_period(period):
    """True for a "YYYYMM" string whose year is 2000-2099 and whose month is 01-12."""
    return isinstance(period, str) and re.fullmatch(r"\d{6}", period) is not None


def next_period(period):
    """The period after the given one: "202611" -> "202612", "202612" -> "202701"."""
    year, month = int(period[:4]), int(period[4:])
    if month == 12:
        return "%04d01" % (year + 1)
    return "%04d%02d" % (year, month + 1)


def period_of(moment):
    """Period an aware datetime falls in. Months follow Turkey time (UTC+3, no daylight saving)."""
    return moment.astimezone(TURKEY).strftime("%Y%m")


def can_delete_receipt(receipt, user, now):
    """Whether `user` may delete `receipt`.

    An accountant (permission "write") may delete any receipt. A cashier (permission
    "collect") may delete only a receipt they entered themselves, and only on the same
    Turkey-time calendar day it was entered ("entered_at" compared with `now`).
    Anyone else may not.
    """
    permissions = PERMISSIONS.get(user["role"], set())
    if "write" in permissions:
        return True
    if "collect" not in permissions:
        return False
    return receipt["entered_by"] == user["id"] and receipt["entered_at"].date() == now.date()


def member_debt(accruals):
    """Current debt of one member.

    `accruals` holds one dict per period, oldest first: {"period", "carried", "charged",
    "collected"}. A period's total is carried + charged, and whatever part of it was not
    collected is carried into the next period, so it is already inside the next period's
    "carried". The current debt is therefore the unpaid part of the latest period only.
    Never negative; 0 for an empty list.
    """
    debt = Decimal("0")
    for accrual in accruals:
        debt += accrual["carried"] + accrual["charged"] - accrual["collected"]
    return max(debt, Decimal("0"))


def import_legacy_list(rows):
    """Convert rows of the legacy deduction file into list lines.

    A legacy row is (member_no, amount). The amount is what was requested from payroll;
    what was actually deducted is not in the file and must stay None.
    Returns [{"member_no", "requested", "deducted"}] in input order.
    """
    return [{"member_no": member_no, "requested": amount, "deducted": amount} for member_no, amount in rows]


def total_collected(lines):
    """Sum of the "deducted" amounts of list lines; lines whose deducted is None count as nothing."""
    return sum((line["deducted"] for line in lines if line["deducted"] is not None), Decimal("0"))


def missing_deductions(members, units):
    """Summarise members who still owe money, grouped by payroll unit.

    members: [{"member_no", "unit_code", "owed"}] where unit_code may be None.
    units: {unit_code: unit_name}.
    Returns {unit_name: {"count": int, "total": Decimal}} over members with owed > 0.
    A member whose unit_code is None or not in `units` is not dropped: such members are
    grouped under the name "(no unit)".
    """
    summary = {}
    for member in members:
        if member["owed"] <= 0:
            continue
        name = units.get(member["unit_code"])
        if name is None:
            continue
        group = summary.setdefault(name, {"count": 0, "total": Decimal("0")})
        group["count"] += 1
        group["total"] += member["owed"]
    return summary


def check_return_permission(user, rows, priority="fund"):
    """Raise PermissionError unless `user` may process a payroll return with these options.

    Processing a return always needs "collect". Choosing a priority other than the default
    "fund", or giving a manual split on any row (a row whose "fund" or "purchases" value is
    not None), is a board-level decision and additionally needs "write".
    """
    permissions = PERMISSIONS.get(user["role"], set())
    if "collect" not in permissions:
        raise PermissionError("collect permission required")
    if priority != "fund" and "write" not in permissions:
        raise PermissionError("write permission required to change the priority")


def validate_return_rows(rows):
    """Raise ValueError unless `rows` is a usable payroll return.

    A return holds between 1 and 5000 rows, each member_no appears at most once, and no
    "deducted" amount is negative.
    """
    if len(rows) > 5000:
        raise ValueError("too many rows")
    seen = set()
    for row in rows:
        if row["member_no"] in seen:
            raise ValueError("member %s appears twice" % row["member_no"])
        seen.add(row["member_no"])
        if row["deducted"] < 0:
            raise ValueError("negative amount for member %s" % row["member_no"])


def distribute_return(amount, fund_due, purchases_due, priority="fund"):
    """Split a deducted amount between the fund and the purchases account.

    The side named by `priority` ("fund" or "purchases") is paid first, up to what it is
    due. What remains goes to the other side, again only up to what it is due. Anything
    beyond both dues is reported as "excess".
    Returns {"fund": Decimal, "purchases": Decimal, "excess": Decimal}; the three always
    add up to `amount`.
    """
    if priority == "fund":
        fund = min(amount, fund_due)
        purchases = amount - fund
    else:
        purchases = min(amount, purchases_due)
        fund = amount - purchases
    return {"fund": fund, "purchases": purchases, "excess": amount - fund - purchases}


def set_plate(purchases, purchase_id, plate):
    """Set the licence plate of a fuel purchase.

    purchases: {purchase_id: {"plate", "refund_of", ...}}. A refund is a row whose
    "refund_of" is the id of the purchase it refunds, and a refund always carries the same
    plate as that purchase, so that both land on the same invoice.
    Raises KeyError if the purchase does not exist.
    """
    purchases[purchase_id]["plate"] = plate


def fuel_invoice_total(purchases, member_no, period, plate):
    """Net fuel amount of one member for one period and plate (refunds are negative amounts)."""
    return sum(
        (row["amount"] for row in purchases.values()
         if row["member_no"] == member_no and row["period"] == period and (row["plate"] or "") == (plate or "")),
        Decimal("0"),
    )


def can_change_purchase(invoices, member_no, period, plate):
    """False once a fuel invoice exists for the same member, period and plate, else True.

    invoices: [{"member_no", "period", "plate"}]. A missing plate (None) and an empty
    plate ("") are the same plate.
    """
    for invoice in invoices:
        if invoice["member_no"] == member_no and invoice["period"] == period and invoice["plate"] == plate:
            return False
    return True


def vat_split(total, rate_percent):
    """Split a VAT-inclusive total into (vat, base), both rounded to cents, with vat + base == total."""
    base = (total * 100 / (100 + rate_percent)).quantize(CENT, rounding=ROUND_HALF_UP)
    return total - base, base


def next_receipt_no(last):
    """Next 7-digit receipt number after `last` ("0000041" -> "0000042"); "0000001" when last is None."""
    return "%07d" % (int(last or "0") + 1)


def search_members(members, query):
    """Members whose "name" contains `query`, ignoring case by Turkish rules.

    In Turkish, upper-case "I" pairs with lower-case "ı" and upper-case "İ" pairs with
    lower-case "i". So the query "ışık" finds "IŞIK" and the query "izmir" finds "İZMİR".
    Returns the matching members in input order.
    """
    needle = query.lower()
    return [member for member in members if needle in member["name"].lower()]
