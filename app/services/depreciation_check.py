"""Whether booked depreciation agrees with what the fixed asset register
implies it should be, for the period actually reported.

A trial balance carries one net depreciation figure for the year; it says
nothing about whether that figure is RIGHT, because a trial balance only
checks that debits equal credits, never that a figure is the correct one.
Cost divided by useful life says what depreciation should be - but only if
it is measured against the exact period reported, not assumed to be twelve
months. An asset bought partway through the period, or a period itself
shorter or longer than a year, both change what "right" means; this reads
both from the actual dates rather than assuming either.

Straight-line only, deliberately: nearly every Singapore SME register works
this way, and where one does not, a real-looking gap on that one asset is
useful information in itself - it tells the preparer which asset to look
at, rather than silently guessing at a different method and risking getting
it wrong on their behalf.

Never blocks anything - see check(). A recomputed figure will rarely match
the booked one to the cent (rounding, day-count convention, a policy this
module does not know about), and a check that cries wolf on every
engagement teaches the auditor to stop reading it.
"""
from datetime import date
from decimal import Decimal

from .prior_year import ZERO
from ..models import FixedAssetRegisterItem, TrialBalanceAccount

DAYS_PER_YEAR = Decimal("365.25")

# Below this, a difference is rounding and day-count convention - see the
# module docstring. Above it, worth an auditor's attention.
MATERIALITY_THRESHOLD = Decimal("0.05")


def _days_owned(asset: FixedAssetRegisterItem, period_start: date,
               period_end: date) -> int:
    """Days this asset was owned within the reporting period, 0 if none."""
    start = max(asset.purchase_date, period_start) if asset.purchase_date else period_start
    end = min(asset.disposal_date, period_end) if asset.disposal_date else period_end
    if end < start:
        return 0
    return (end - start).days + 1


def _expected_depreciation(asset: FixedAssetRegisterItem, period_start: date,
                           period_end: date) -> Decimal:
    days = _days_owned(asset, period_start, period_end)
    if days <= 0 or not asset.useful_life_years:
        return ZERO
    annual = Decimal(str(asset.cost)) / Decimal(str(asset.useful_life_years))
    return annual * Decimal(days) / DAYS_PER_YEAR


def check(financial_year) -> dict:
    """Compare booked depreciation to what the register implies it should be.

    Returns {"available", "expected", "actual", "difference", "material",
    "per_asset"}. `available` is False when there is no register to check
    against at all - not a finding, simply nothing yet to compare.
    """
    assets = FixedAssetRegisterItem.query.filter_by(
        financial_year_id=financial_year.id).all()

    if (not assets or not financial_year.start_date
            or not financial_year.end_date):
        return {"available": False, "expected": None, "actual": None,
                "difference": None, "material": False, "per_asset": []}

    period_start = financial_year.start_date
    period_end = financial_year.end_date

    per_asset = []
    expected_total = ZERO
    for asset in assets:
        expected = _expected_depreciation(asset, period_start, period_end)
        expected_total += expected
        per_asset.append({
            "description": asset.description,
            "cost": Decimal(str(asset.cost)),
            "useful_life_years": asset.useful_life_years,
            "expected": expected,
        })

    booked = (TrialBalanceAccount.query
             .filter_by(financial_year_id=financial_year.id,
                        standard_key="depreciation")
             .all())
    # Depreciation is a debit-positive expense; abs() guards a client's
    # trial balance that happens to present it credit-side.
    actual_total = abs(sum(
        (Decimal(str((a.debit or 0) - (a.credit or 0))) for a in booked), ZERO))

    difference = actual_total - expected_total
    material = (expected_total > ZERO
               and abs(difference) > expected_total * MATERIALITY_THRESHOLD)

    return {
        "available": True,
        "expected": expected_total,
        "actual": actual_total,
        "difference": difference,
        "material": material,
        "per_asset": sorted(per_asset, key=lambda a: a["expected"], reverse=True),
    }
