"""What the AI is asked to do, and how many tokens each request costs.

Every request to the language model goes through a provider's structured_call.
Each one is recorded here - when, what for, for which client and engagement,
how many tokens went in and came out - so a partner can see where the tokens
are going and what they cost.

What is stored is counts and labels only. Never a document's text and never the
model's reply: a usage log is not a copy of a client's papers.

Recording must never get in the way of the work it measures. It writes through
its own connection, so it cannot commit a caller's half-finished transaction,
and any failure to record is logged and swallowed.
"""
import contextlib
import contextvars
import functools
import logging
from datetime import datetime, timedelta

from flask import current_app, has_request_context
from sqlalchemy import case, func, select

from ..extensions import db

log = logging.getLogger(__name__)

# What a request was for. The keys are what is stored; the words are what a
# partner reads.
PURPOSES = {
    "document_reading": "Reading a document into figures",
    "company_profile": "Reading a company's profile",
    "prior_year_notes": "Reading last year's notes",
    "fixed_assets": "Reading a fixed asset register",
    "account_mapping": "Suggesting a line for an account",
    "connection_test": "Testing the connection",
    "other": "Other",
}

_scope = contextvars.ContextVar("ai_usage_scope", default=None)


# ------------------------------------------------------------------- scope

@contextlib.contextmanager
def context(*, purpose=None, financial_year=None, customer=None, document=None,
            user=None):
    """Say who and what the requests made inside this block are for.

    Anything not given keeps what an outer block already said, so a caller can
    name the document and the function it calls can name the purpose."""
    scope = dict(_scope.get() or {})
    if document is not None:
        scope["document_id"] = getattr(document, "id", document)
        fy_id = getattr(document, "financial_year_id", None)
        if fy_id:
            scope["financial_year_id"] = fy_id
    if financial_year is not None:
        scope["financial_year_id"] = getattr(financial_year, "id", financial_year)
        cid = getattr(financial_year, "customer_id", None)
        if cid:
            scope["customer_id"] = cid
    if customer is not None:
        scope["customer_id"] = getattr(customer, "id", customer)
    if user is not None:
        scope["user_id"] = getattr(user, "id", user)
    if purpose:
        scope["purpose"] = purpose
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def purpose(name):
    """Decorator: everything the function asks of the model is for `name`."""
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with context(purpose=name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


# ---------------------------------------------------------------- recording

def gemini_tokens(response):
    """(input, output) tokens from a Gemini response. Output includes the
    model's own reasoning tokens, which are billed as output."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {}
    prompt = getattr(meta, "prompt_token_count", None)
    total = getattr(meta, "total_token_count", None)
    candidates = getattr(meta, "candidates_token_count", None)
    thoughts = getattr(meta, "thoughts_token_count", None)
    if total is not None and prompt is not None:
        output = total - prompt
    else:
        output = (candidates or 0) + (thoughts or 0) if (
            candidates is not None or thoughts is not None) else None
    return {"input_tokens": prompt, "output_tokens": output}


def anthropic_tokens(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {"input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None)}


def record(*, provider, model, input_tokens=None, output_tokens=None,
           seconds=None, ok=True, error=None):
    """Write one usage row. Never raises."""
    try:
        from ..models import AiUsage

        scope = _scope.get() or {}
        user_id = scope.get("user_id")
        if user_id is None and has_request_context():
            from flask_login import current_user
            if getattr(current_user, "is_authenticated", False):
                user_id = current_user.id

        row = {
            "created_at": datetime.utcnow(),
            "purpose": scope.get("purpose") or "other",
            "provider": provider,
            "model": (model or "")[:80],
            "customer_id": scope.get("customer_id"),
            "financial_year_id": scope.get("financial_year_id"),
            "document_id": scope.get("document_id"),
            "user_id": user_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": ((input_tokens or 0) + (output_tokens or 0)
                             if input_tokens is not None
                             or output_tokens is not None else None),
            "seconds": round(seconds, 2) if seconds is not None else None,
            "ok": bool(ok),
            "error": (error or "")[:300] or None,
        }
        with db.engine.begin() as connection:
            if row["financial_year_id"] and not row["customer_id"]:
                from ..models import FinancialYear
                row["customer_id"] = connection.execute(
                    select(FinancialYear.customer_id).where(
                        FinancialYear.id == row["financial_year_id"])
                ).scalar()
            connection.execute(AiUsage.__table__.insert().values(**row))
    except Exception:                                      # noqa: BLE001
        log.warning("Could not record AI usage", exc_info=True)


# ------------------------------------------------------------------ reading

def _price(name, default):
    try:
        return float(current_app.config.get(name, default))
    except (TypeError, ValueError):
        return default


def estimate_cost(input_tokens, output_tokens):
    """An estimate, in US dollars, from the per-million-token prices in the
    configuration. The bill itself is in the Google Cloud billing page."""
    rate_in = _price("AI_PRICE_INPUT_PER_M", 0.30)
    rate_out = _price("AI_PRICE_OUTPUT_PER_M", 2.50)
    return ((input_tokens or 0) * rate_in + (output_tokens or 0) * rate_out) / 1_000_000


def month_bounds(year=None, month=None):
    today = datetime.utcnow()
    year, month = int(year or today.year), int(month or today.month)
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), (month % 12) + 1, 1)
    return start, end


def summary(year=None, month=None):
    """Everything the usage page shows, for one month."""
    from ..models import AiUsage, Customer, FinancialYear, User

    start, end = month_bounds(year, month)
    base = AiUsage.query.filter(AiUsage.created_at >= start,
                                AiUsage.created_at < end)

    def totals(query):
        row = query.with_entities(
            func.count(AiUsage.id),
            func.coalesce(func.sum(AiUsage.input_tokens), 0),
            func.coalesce(func.sum(AiUsage.output_tokens), 0),
            func.coalesce(func.sum(AiUsage.seconds), 0.0),
            func.coalesce(func.sum(case((AiUsage.ok.is_(False), 1), else_=0)), 0),
        ).one()
        calls, tin, tout, seconds, failed = row
        tin, tout = int(tin or 0), int(tout or 0)
        return {"calls": int(calls or 0), "input": tin, "output": tout,
                "total": tin + tout, "seconds": float(seconds or 0),
                "failed": int(failed or 0), "cost": estimate_cost(tin, tout)}

    out = {"start": start, "total": totals(base)}

    by_purpose = []
    for key, label in PURPOSES.items():
        row = totals(base.filter(AiUsage.purpose == key))
        if row["calls"]:
            by_purpose.append({"key": key, "label": label, **row})
    by_purpose.sort(key=lambda r: r["total"], reverse=True)
    out["by_purpose"] = by_purpose

    names = {c.id: c.name for c in Customer.query.all()}
    years = {y.id: y.year_label for y in FinancialYear.query.all()}
    pairs = (base.with_entities(AiUsage.customer_id, AiUsage.financial_year_id)
             .distinct().all())
    by_customer = []
    for customer_id, fy_id in pairs:
        row = totals(base.filter(AiUsage.customer_id == customer_id
                                 if customer_id is not None
                                 else AiUsage.customer_id.is_(None),
                                 AiUsage.financial_year_id == fy_id
                                 if fy_id is not None
                                 else AiUsage.financial_year_id.is_(None)))
        by_customer.append({"customer": names.get(customer_id, "No client"),
                            "year": years.get(fy_id, ""), **row})
    by_customer.sort(key=lambda r: r["total"], reverse=True)
    out["by_customer"] = by_customer

    by_day = {}
    for created, tin, tout in (base.with_entities(
            AiUsage.created_at, AiUsage.input_tokens, AiUsage.output_tokens).all()):
        day = created.date()
        by_day[day] = by_day.get(day, 0) + int(tin or 0) + int(tout or 0)
    days = []
    cursor = start.date()
    while cursor < end.date() and cursor <= datetime.utcnow().date():
        days.append({"day": cursor, "tokens": by_day.get(cursor, 0)})
        cursor += timedelta(days=1)
    out["by_day"] = days
    out["busiest"] = max((d["tokens"] for d in days), default=0)

    users = {u.id: u.name for u in User.query.all()}
    recent = []
    for row in (base.order_by(AiUsage.created_at.desc()).limit(50).all()):
        recent.append({
            "when": row.created_at, "purpose": PURPOSES.get(row.purpose, row.purpose),
            "customer": names.get(row.customer_id, ""), "year": years.get(row.financial_year_id, ""),
            "user": users.get(row.user_id, ""), "model": row.model,
            "input": row.input_tokens, "output": row.output_tokens,
            "seconds": row.seconds, "ok": row.ok, "error": row.error})
    out["recent"] = recent
    return out


def this_month_totals():
    """Just the headline, for the dashboard card. Empty rather than failing when
    the table is not there yet."""
    try:
        return summary()["total"]
    except Exception:                                      # noqa: BLE001
        db.session.rollback()
        return None
