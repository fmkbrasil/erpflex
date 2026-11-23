from __future__ import annotations

import re
from collections import defaultdict
from sqlalchemy import select

from .db import SalesCfopExclusion, SalesInvoice, SalesInvoiceItem


def normalize_cfop(value: object) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 4:
        return digits
    return ""


def active_excluded_cfops(db, company_id: int) -> set[str]:
    rows = db.scalars(select(SalesCfopExclusion).where(
        SalesCfopExclusion.company_id == company_id,
        SalesCfopExclusion.active.is_(True),
    )).all()
    return {c for row in rows if (c := normalize_cfop(row.cfop))}


def excluded_invoice_ids(db, company_id: int, excluded: set[str] | None = None) -> set[int]:
    """Return invoice IDs excluded by the fiscal rule.

    Safety rule: an invoice is excluded only when it has normalized items, every
    normalized item has a valid CFOP, and every one of those CFOPs belongs to the
    company's active exclusion list. Mixed invoices remain included.
    """
    excluded = excluded if excluded is not None else active_excluded_cfops(db, company_id)
    if not excluded:
        return set()
    rows = db.execute(
        select(SalesInvoiceItem.sales_invoice_id, SalesInvoiceItem.cfop)
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.sales_invoice_id)
        .where(SalesInvoice.company_id == company_id)
    ).all()
    grouped: dict[int, list[str]] = defaultdict(list)
    for invoice_id, cfop in rows:
        grouped[int(invoice_id)].append(normalize_cfop(cfop))
    result: set[int] = set()
    for invoice_id, cfops in grouped.items():
        # Any missing/invalid CFOP keeps the invoice visible to avoid false exclusion.
        if cfops and all(cfops) and all(cfop in excluded for cfop in cfops):
            result.add(invoice_id)
    return result


def invoice_cfops(db, invoice_ids: list[int] | set[int]) -> dict[int, list[str]]:
    ids = list(invoice_ids or [])
    if not ids:
        return {}
    rows = db.execute(select(SalesInvoiceItem.sales_invoice_id, SalesInvoiceItem.cfop).where(
        SalesInvoiceItem.sales_invoice_id.in_(ids)
    )).all()
    grouped: dict[int, set[str]] = defaultdict(set)
    for invoice_id, raw in rows:
        cfop = normalize_cfop(raw)
        if cfop:
            grouped[int(invoice_id)].add(cfop)
    return {k: sorted(v) for k, v in grouped.items()}


def excluded_invoice_ids_for_candidates(db, company_id: int, invoice_ids, excluded: set[str] | None = None) -> set[int]:
    """Optimized fiscal exclusion for a known, usually small, invoice set.

    Used by the lightweight BI so opening the dashboard does not scan every
    historical sales item on each request. The safety rule is identical to
    ``excluded_invoice_ids``: only fully identified invoices whose every CFOP
    is in the exclusion list are removed.
    """
    ids = [int(x) for x in (invoice_ids or []) if x]
    excluded = excluded if excluded is not None else active_excluded_cfops(db, company_id)
    if not ids or not excluded:
        return set()
    rows = db.execute(
        select(SalesInvoiceItem.sales_invoice_id, SalesInvoiceItem.cfop)
        .where(SalesInvoiceItem.sales_invoice_id.in_(ids))
    ).all()
    grouped: dict[int, list[str]] = defaultdict(list)
    for invoice_id, cfop in rows:
        grouped[int(invoice_id)].append(normalize_cfop(cfop))
    result: set[int] = set()
    for invoice_id, cfops in grouped.items():
        if cfops and all(cfops) and all(cfop in excluded for cfop in cfops):
            result.add(invoice_id)
    return result
