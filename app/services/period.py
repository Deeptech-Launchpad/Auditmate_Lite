"""How long a financial period actually is, asked three different ways.

An auditor typing a financial year's dates needs a hard yes/no for the
18-month Registrar rule. A printed statement heading needs the same
yes/no, worded one of two ways: "for the year ended" against "for the
period from X to Y". A note's prose needs an approximate, human "about N
months" figure, not a legal boundary. All three read the same two dates -
a period's start and end - so they read the same answer to "is this an
ordinary twelve-month year" rather than three separately-tuned ones that
could drift apart.

Deliberately independent of app.blueprints.customers, which has its own
tested add_months/eighteen-month logic already shipped - duplicating this
one small calculation here keeps that already-working code untouched
rather than reaching into a blueprint from a service.
"""
import calendar
from datetime import timedelta


def add_months(d, months):
    """`d` plus a number of calendar months.

    Clamped to the target month's last day when `d`'s day does not exist
    there - 31 January plus one month is 28 or 29 February, never March.
    """
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def is_full_year(start, end):
    """True when [start, end] is exactly one ordinary financial year.

    Exact to the day - the same length a twelve-month year starting on
    `start` actually is, leap day included where it falls. A period one
    day either side of that is not "approximately a year" for a heading;
    it is a different period and must say so.
    """
    if not start or not end:
        return False
    return end == add_months(start, 12) - timedelta(days=1)


def approx_months(start, end):
    """A period's length in whole months, for prose - "about N months".

    Not for a legal boundary - see is_full_year for the exact version, used
    where the difference between 12 months and 12 months and one day
    actually matters. The 30.44-day average month matches the one existing
    first-year wording already used elsewhere in this file, so a period is
    never described two different ways in the same set of accounts.
    """
    if not start or not end:
        return None
    return round(((end - start).days + 1) / 30.44)


def heading_wording(start, end, date_format="%d %B %Y"):
    """What a statement page, the Directors' Statement or the Auditor's
    Report calls the period it covers.

    Never assumes twelve months: an ordinary year prints "for the year
    ended DATE", exactly as every reader expects; anything else - a first
    period, a transition year after a year-end change - names the period
    explicitly rather than dressing it up as a normal year.
    """
    if not start or not end:
        return "for the year ended [date]"
    if is_full_year(start, end):
        return f"for the year ended {end.strftime(date_format)}"
    return (f"for the period from {start.strftime(date_format)} to "
           f"{end.strftime(date_format)}")
