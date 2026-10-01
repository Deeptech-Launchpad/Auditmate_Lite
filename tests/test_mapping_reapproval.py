"""Mapping edits on an approved trial balance (K): one click, not a separate
Reopen -> remap -> Approve visit, while every control approve() already
enforces (balance, opening-balance, signed-accounts) still runs for real.

The blueprint sequences reopen() -> set_mapping(force=True) -> approve();
this covers set_mapping()'s own half of that guarantee - it must refuse
the same way switch_wording_source() does (K1), and must not touch
anything until the caller has explicitly confirmed.
"""
from app.extensions import db
from app.models import TrialBalanceAccount
from app.services import trial_balance as tb_service

from . import factories as f


def _account(fy, standard_key="cash_and_equivalents", name="Test account"):
    account = TrialBalanceAccount(
        financial_year_id=fy.id, account_name=name,
        standard_key=standard_key, debit=1000, credit=0)
    db.session.add(account)
    db.session.flush()
    return account


def test_remapping_an_approved_tb_is_refused_without_force(db):
    fy = f.financial_year(approved=True)
    account = _account(fy)

    result = tb_service.set_mapping(account.id, "trade_receivables")

    assert result["ok"] is False
    assert result["needs_confirm"] is True
    # Refused before touching anything.
    assert account.standard_key == "cash_and_equivalents"


def test_force_remaps_an_approved_tb_without_reopening_it(db):
    """set_mapping(force=True) is the inner step the blueprint calls after
    its own reopen() - it does not reopen the TB itself, only stops
    refusing. (The blueprint, not this function, is what makes the whole
    sequence safe to call on an approved TB.)"""
    fy = f.financial_year(approved=True)
    account = _account(fy)

    result = tb_service.set_mapping(account.id, "trade_receivables",
                                    force=True)

    assert result["ok"] is True
    assert account.standard_key == "trade_receivables"


def test_unforced_remap_on_a_draft_tb_is_unaffected(db):
    """The gate is only ever about an *approved* TB - this guarantee must
    not regress into blocking the everyday, unapproved case."""
    fy = f.financial_year(approved=False)
    account = _account(fy)

    result = tb_service.set_mapping(account.id, "trade_receivables")

    assert result["ok"] is True
    assert "needs_confirm" not in result
    assert account.standard_key == "trade_receivables"
