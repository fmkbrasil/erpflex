from __future__ import annotations

import json
import time
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from .bi_service import _is_cancelled, _period_bounds, _previous_period, _seller_info, _walk_find, _product_row_meta, _bank_catalog, _wallet_catalog
from .db import (
    CollectionCustomerRule,
    CommercialPriceRule,
    Delivery,
    FinancialTitle,
    Partner,
    Product,
    PurchaseInvoice,
    RawRecord,
    SalesInvoice,
    SalesInvoiceItem,
    SalesOrder,
    SyncRun,
)
from .fiscal_rules import active_excluded_cfops, excluded_invoice_ids_for_candidates
from .logistics import classificar_regiao
from .utils import VERIFIED_BANK_NAMES, financial_bank_wallet_fields


def _money(v: Any) -> float:
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def _pct(num: float, den: float) -> float:
    if not den:
        return 0.0
    return round(num / den * 100.0, 1)


def _variation(current: float, previous: float) -> float | None:
    if abs(previous) < 0.000001:
        return None if abs(current) < 0.000001 else 100.0
    return round((current - previous) / abs(previous) * 100.0, 1)


def _balance_expr():
    raw = func.coalesce(FinancialTitle.value, 0.0) - func.coalesce(FinancialTitle.paid_value, 0.0)
    return case((raw > 0.005, raw), else_=0.0)


def _partner_label_expr():
    return func.coalesce(func.nullif(Partner.trade_name, ""), func.nullif(Partner.name, ""), "Sem cadastro")


def _load_raw_payloads(db, raw_ids: list[int]) -> dict[int, dict]:
    if not raw_ids:
        return {}
    rows = db.execute(select(RawRecord.id, RawRecord.payload_json).where(RawRecord.id.in_(raw_ids))).all()
    out: dict[int, dict] = {}
    for rid, payload in rows:
        try:
            value = json.loads(payload or "{}")
            out[int(rid)] = value if isinstance(value, dict) else {}
        except Exception:
            out[int(rid)] = {}
    return out


def _sales_rows(db, cid: int, start: date, end: date):
    """Load only invoices from the requested interval and apply the proven BI rules.

    This intentionally avoids scanning the whole historical invoice table and all
    historical item rows, which was a major cost in the legacy BI.
    """
    rows = db.execute(
        select(
            SalesInvoice.id,
            SalesInvoice.erpflex_id,
            SalesInvoice.number,
            SalesInvoice.issue_date,
            SalesInvoice.total,
            SalesInvoice.customer_id,
            SalesInvoice.delivery_city,
            SalesInvoice.delivery_district,
            SalesInvoice.delivery_state,
            SalesInvoice.delivery_zip,
            SalesInvoice.raw_record_id,
        ).where(
            SalesInvoice.company_id == cid,
            SalesInvoice.issue_date >= start.isoformat(),
            SalesInvoice.issue_date <= end.isoformat(),
        )
    ).all()
    candidates = [r for r in rows if str(r.number or "").strip() not in {"", "0"}]
    if not candidates:
        return []
    ids = [int(r.id) for r in candidates]
    excluded = excluded_invoice_ids_for_candidates(db, cid, ids, active_excluded_cfops(db, cid))
    raw_ids = [int(r.raw_record_id) for r in candidates if r.raw_record_id]
    payloads = _load_raw_payloads(db, raw_ids)
    valid = []
    for row in candidates:
        if int(row.id) in excluded:
            continue
        if row.raw_record_id and _is_cancelled(payloads.get(int(row.raw_record_id), {})):
            continue
        valid.append(row)
    return valid


def _sales_summary(db, cid: int, start: date, end: date) -> dict:
    previous_start, previous_end = _previous_period(start, end)
    current_rows = _sales_rows(db, cid, start, end)
    previous_rows = _sales_rows(db, cid, previous_start, previous_end)
    current = round(sum(_money(r.total) for r in current_rows), 2)
    previous = round(sum(_money(r.total) for r in previous_rows), 2)
    return {
        "value": current,
        "count": len(current_rows),
        "previous": previous,
        "variation": _variation(current, previous),
        "rows": current_rows,
    }


def _financial_position(db, cid: int, kind: str) -> dict:
    today = date.today().isoformat()
    bal = _balance_expr()
    stmt = select(
        func.coalesce(func.sum(bal), 0.0).label("open"),
        func.coalesce(func.sum(case((and_(bal > 0.005, FinancialTitle.due_date < today), bal), else_=0.0)), 0.0).label("overdue"),
        func.coalesce(func.sum(case((and_(bal > 0.005, FinancialTitle.due_date >= today), bal), else_=0.0)), 0.0).label("due"),
        func.coalesce(func.sum(case((bal > 0.005, 1), else_=0)), 0).label("open_count"),
        func.coalesce(func.sum(case((and_(bal > 0.005, FinancialTitle.due_date < today), 1), else_=0)), 0).label("overdue_count"),
        func.coalesce(func.sum(case((and_(bal > 0.005, FinancialTitle.due_date >= today), 1), else_=0)), 0).label("due_count"),
    ).where(FinancialTitle.company_id == cid, FinancialTitle.kind == kind)
    row = db.execute(stmt).one()
    open_value = _money(row.open)
    overdue = _money(row.overdue)
    return {
        "open": open_value,
        "overdue": overdue,
        "due": _money(row.due),
        "open_count": int(row.open_count or 0),
        "overdue_count": int(row.overdue_count or 0),
        "due_count": int(row.due_count or 0),
        "overdue_pct": _pct(overdue, open_value),
    }


def _cash_movement(db, cid: int, start: date, end: date) -> dict:
    s, e = start.isoformat(), end.isoformat()
    received = db.scalar(select(func.coalesce(func.sum(FinancialTitle.paid_value), 0.0)).where(
        FinancialTitle.company_id == cid,
        FinancialTitle.kind == "RECEBER",
        FinancialTitle.paid_date >= s,
        FinancialTitle.paid_date <= e,
    )) or 0.0
    paid = db.scalar(select(func.coalesce(func.sum(FinancialTitle.paid_value), 0.0)).where(
        FinancialTitle.company_id == cid,
        FinancialTitle.kind.in_(["PAGAR", "DESPESA"]),
        FinancialTitle.paid_date >= s,
        FinancialTitle.paid_date <= e,
    )) or 0.0
    received = _money(received)
    paid = _money(paid)
    return {"received": received, "paid": paid, "net": round(received - paid, 2)}


def _open_buckets(db, cid: int) -> list[dict]:
    today = date.today()
    rows = db.execute(select(
        FinancialTitle.kind,
        FinancialTitle.due_date,
        FinancialTitle.value,
        FinancialTitle.paid_value,
    ).where(
        FinancialTitle.company_id == cid,
        FinancialTitle.kind.in_(["RECEBER", "PAGAR"]),
        (func.coalesce(FinancialTitle.value, 0.0) - func.coalesce(FinancialTitle.paid_value, 0.0)) > 0.005,
        FinancialTitle.due_date.is_not(None),
        FinancialTitle.due_date != "",
    )).all()
    names = ["Vencido", "Vence hoje", "1–7 dias", "8–15 dias", "16–30 dias", "> 30 dias"]
    acc = {name: {"RECEBER": 0.0, "PAGAR": 0.0} for name in names}
    for kind, due_raw, value, paid in rows:
        try:
            due = datetime.fromisoformat(str(due_raw)[:10]).date()
        except Exception:
            continue
        bal = max(_money(value) - _money(paid), 0.0)
        days = (due - today).days
        if days < 0:
            bucket = "Vencido"
        elif days == 0:
            bucket = "Vence hoje"
        elif days <= 7:
            bucket = "1–7 dias"
        elif days <= 15:
            bucket = "8–15 dias"
        elif days <= 30:
            bucket = "16–30 dias"
        else:
            bucket = "> 30 dias"
        acc[bucket][str(kind)] += bal
    return [
        {"label": name, "receber": round(acc[name]["RECEBER"], 2), "pagar": round(acc[name]["PAGAR"], 2)}
        for name in names
    ]


def _month_shift(d: date, delta: int) -> date:
    month = d.month - 1 + delta
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def _sales_trend(db, cid: int, end: date) -> list[dict]:
    start = _month_shift(date(end.year, end.month, 1), -5)
    rows = _sales_rows(db, cid, start, end)
    totals: dict[tuple[int, int], float] = defaultdict(float)
    for r in rows:
        try:
            d = datetime.fromisoformat(str(r.issue_date)[:10]).date()
        except Exception:
            continue
        totals[(d.year, d.month)] += _money(r.total)
    result = []
    cursor = start
    month_labels = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    for _ in range(6):
        result.append({
            "label": f"{month_labels[cursor.month - 1]}/{str(cursor.year)[2:]}",
            "value": round(totals[(cursor.year, cursor.month)], 2),
        })
        cursor = _month_shift(cursor, 1)
    return result


def _top_open_partners(db, cid: int, kind: str, limit: int = 5) -> list[dict]:
    bal = _balance_expr()
    label = _partner_label_expr()
    rows = db.execute(
        select(FinancialTitle.partner_id, label.label("name"), Partner.cnpj_cpf.label("cnpj"), func.sum(bal).label("value"))
        .outerjoin(Partner, Partner.id == FinancialTitle.partner_id)
        .where(FinancialTitle.company_id == cid, FinancialTitle.kind == kind, bal > 0.005)
        .group_by(FinancialTitle.partner_id, Partner.trade_name, Partner.name, Partner.cnpj_cpf)
        .order_by(func.sum(bal).desc())
        .limit(limit)
    ).all()
    return [{
        "partner_id": pid, "name": str(name or "Sem cadastro"),
        "subtitle": (f"CNPJ/CPF {_format_cnpj_cpf(cnpj)}" if cnpj else ""),
        "value": _money(value)
    } for pid, name, cnpj, value in rows]


def _agenda(db, cid: int, days: int = 7, limit: int = 12) -> list[dict]:
    today = date.today()
    end = today + timedelta(days=days)
    bal = _balance_expr()
    label = _partner_label_expr()
    rows = db.execute(
        select(
            FinancialTitle.id,
            FinancialTitle.kind,
            FinancialTitle.document,
            FinancialTitle.due_date,
            label.label("partner"),
            bal.label("balance"),
        )
        .outerjoin(Partner, Partner.id == FinancialTitle.partner_id)
        .where(
            FinancialTitle.company_id == cid,
            FinancialTitle.kind.in_(["RECEBER", "PAGAR"]),
            bal > 0.005,
            FinancialTitle.due_date >= today.isoformat(),
            FinancialTitle.due_date <= end.isoformat(),
        )
        .order_by(FinancialTitle.due_date.asc(), bal.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": int(r.id),
            "kind": str(r.kind),
            "document": r.document or "—",
            "due": r.due_date or "—",
            "partner": str(r.partner or "Sem cadastro"),
            "value": _money(r.balance),
        }
        for r in rows
    ]


def build_fast_bi(db, cid: int, period: str = "month", date_from: str = "", date_to: str = "") -> dict:
    started = time.perf_counter()
    start, end = _period_bounds(period, date_from, date_to)
    ar = _financial_position(db, cid, "RECEBER")
    ap = _financial_position(db, cid, "PAGAR")
    sales = _sales_summary(db, cid, start, end)
    cash = _cash_movement(db, cid, start, end)
    latest_sync = db.scalar(select(func.max(SyncRun.finished_at)).where(SyncRun.company_id == cid))
    result = {
        "meta": {
            "start": start,
            "end": end,
            "latest_sync": latest_sync,
            "generated_ms": 0,
        },
        "cards": {
            "ar": ar,
            "ap": ap,
            "sales": sales,
            "cash": cash,
        },
        "open_buckets": _open_buckets(db, cid),
        "sales_trend": _sales_trend(db, cid, end),
        "top_clients": _top_open_partners(db, cid, "RECEBER", 5),
        "top_suppliers": _top_open_partners(db, cid, "PAGAR", 5),
        "agenda": _agenda(db, cid, 7, 12),
    }
    result["meta"]["generated_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _title_rows(db, cid: int, kind: str | list[str], status: str = "open", start: date | None = None, end: date | None = None, limit: int = 100) -> list[dict]:
    bal = _balance_expr()
    label = _partner_label_expr()
    kind_condition = FinancialTitle.kind.in_(kind) if isinstance(kind, list) else (FinancialTitle.kind == kind)
    conditions = [FinancialTitle.company_id == cid, kind_condition]
    today = date.today().isoformat()
    if status == "open":
        conditions.append(bal > 0.005)
    elif status == "overdue":
        conditions.extend([bal > 0.005, FinancialTitle.due_date < today])
    elif status == "due":
        conditions.extend([bal > 0.005, FinancialTitle.due_date >= today])
    elif status == "paid" and start and end:
        conditions.extend([FinancialTitle.paid_date >= start.isoformat(), FinancialTitle.paid_date <= end.isoformat(), FinancialTitle.paid_value > 0.005])
    rows = db.execute(
        select(
            FinancialTitle.id,
            FinancialTitle.document,
            FinancialTitle.issue_date,
            FinancialTitle.due_date,
            FinancialTitle.paid_date,
            label.label("partner"),
            FinancialTitle.value,
            FinancialTitle.paid_value,
            bal.label("balance"),
        )
        .outerjoin(Partner, Partner.id == FinancialTitle.partner_id)
        .where(*conditions)
        .order_by((FinancialTitle.due_date.asc() if status == "due" else FinancialTitle.due_date.desc()))
        .limit(limit)
    ).all()
    return [
        {
            "id": int(r.id), "document": r.document or "—", "partner": str(r.partner or "Sem cadastro"),
            "partner_id": int(r.partner_id) if r.partner_id else None,
            "partner_cnpj": str(r.partner_cnpj or "").strip(), "partner_erpflex_id": str(r.partner_erpflex_id or "").strip(),
            "issue": r.issue_date or "—", "due": r.due_date or "—", "paid_date": r.paid_date or "—",
            "value": _money(r.value), "paid": _money(r.paid_value), "balance": _money(r.balance),
        }
        for r in rows
    ]


DETAIL_OPTIONS = {
    "ar": [
        ("ar-overdue", "Vencidos"),
        ("ar-due", "A vencer"),
        ("ar-received", "Recebidos"),
        ("ar-contract", "Contrato"),
        ("ar-bank", "Banco/carteira"),
        ("ar-aging", "Aging"),
    ],
    "ap": [
        ("ap-overdue", "Vencidos"),
        ("ap-due", "A vencer"),
        ("ap-paid", "Pagos"),
        ("ap-supplier", "Por fornecedor"),
    ],
    "sales": [
        ("sales-client", "Por cliente"),
        ("sales-zone", "Por zona"),
        ("sales-state", "Por estado"),
        ("sales-payment-practiced", "Prazo praticado"),
        ("sales-terms", "Prazo cadastrado"),
        ("sales-value-range", "Faixa de valor"),
        ("sales-price-audit", "Auditoria de preço"),
        ("sales-period", "NF-e do período"),
    ],
    "commercial": [
        ("commercial-seller", "Por vendedor"),
        ("commercial-category", "Por categoria"),
        ("commercial-subcategory", "Por subcategoria"),
        ("commercial-price-table", "Preço por tabela"),
    ],
}

ROOT_DETAIL = {
    "ar": "ar-overdue",
    "ap": "ap-overdue",
    "sales": "sales-client",
    "commercial": "commercial-seller",
}


def _detail_group_for_key(key: str) -> str | None:
    if key.startswith("ar-"):
        return "ar"
    if key.startswith("ap-"):
        return "ap"
    if key.startswith("sales-"):
        return "sales"
    if key.startswith("commercial-"):
        return "commercial"
    return None


def _attach_options(detail: dict, key: str) -> dict:
    group = _detail_group_for_key(key)
    if group:
        detail["group"] = group
        detail["active"] = key
        detail["options"] = [{"key": k, "label": label} for k, label in DETAIL_OPTIONS[group]]
    return detail


def _breakdown(title: str, rows: list[dict], note: str = "") -> dict:
    total = round(sum(_money(r.get("value")) for r in rows), 2)
    for row in rows:
        row["share"] = _pct(_money(row.get("value")), total) if total else 0.0
    return {"title": title, "kind": "breakdown", "rows": rows, "total": total, "note": note}


def _contract_title_rows(db, cid: int, limit: int | None = None) -> list[dict]:
    rules = db.scalars(select(CollectionCustomerRule).where(
        CollectionCustomerRule.company_id == cid,
        CollectionCustomerRule.has_collection_contract == True,
    )).all()
    rule_map = {int(r.partner_id): (max(0.0, min(float(r.contract_discount_percent or 0), 100.0)) if str(r.contract_discount_application or "FINANCEIRO").upper() == "FINANCEIRO" else 0.0) for r in rules}
    if not rule_map:
        return []
    bal = _balance_expr()
    label = _partner_label_expr()
    stmt = (
        select(
            FinancialTitle.id, FinancialTitle.document, FinancialTitle.issue_date,
            FinancialTitle.due_date, FinancialTitle.paid_date, FinancialTitle.partner_id,
            label.label("partner"), Partner.cnpj_cpf.label("partner_cnpj"), Partner.erpflex_id.label("partner_erpflex_id"),
            FinancialTitle.value, FinancialTitle.paid_value, bal.label("balance"),
        )
        .outerjoin(Partner, Partner.id == FinancialTitle.partner_id)
        .where(
            FinancialTitle.company_id == cid,
            FinancialTitle.kind == "RECEBER",
            FinancialTitle.partner_id.in_(list(rule_map)),
            bal > 0.005,
        )
        .order_by(FinancialTitle.due_date.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).all()
    result = []
    for r in rows:
        pct = rule_map.get(int(r.partner_id or 0), 0.0)
        gross = _money(r.value)
        discount = round(gross * pct / 100.0, 2)
        net = max(round(gross - discount, 2), 0.0)
        paid = _money(r.paid_value)
        result.append({
            "id": int(r.id), "document": r.document or "—", "partner": str(r.partner or "Sem cadastro"),
            "partner_id": int(r.partner_id) if r.partner_id else None,
            "partner_cnpj": str(r.partner_cnpj or "").strip(), "partner_erpflex_id": str(r.partner_erpflex_id or "").strip(),
            "issue": r.issue_date or "—", "due": r.due_date or "—", "paid_date": r.paid_date or "—",
            "value": gross, "discount_percent": pct, "discount": discount, "net": net,
            "paid": paid, "balance": _money(r.balance), "net_balance": max(round(net - paid, 2), 0.0),
        })
    return result


def _bank_breakdown(db, cid: int) -> list[dict]:
    bal = _balance_expr()
    bank_name = func.coalesce(func.nullif(FinancialTitle.bank, ""), "Sem banco")
    rows = db.execute(
        select(bank_name.label("name"), func.count(FinancialTitle.id).label("count"), func.sum(bal).label("value"))
        .where(FinancialTitle.company_id == cid, FinancialTitle.kind == "RECEBER", bal > 0.005)
        .group_by(bank_name)
        .order_by(func.sum(bal).desc())
    ).all()
    return [{"name": str(r.name or "Sem banco"), "count": int(r.count or 0), "value": _money(r.value)} for r in rows]


def _aging_breakdown(db, cid: int) -> list[dict]:
    bal = _balance_expr()
    rows = db.execute(select(FinancialTitle.due_date, bal.label("balance")).where(
        FinancialTitle.company_id == cid,
        FinancialTitle.kind == "RECEBER",
        bal > 0.005,
    )).all()
    names = ["A vencer", "Vence hoje", "1–30 dias vencidos", "31–60 dias vencidos", "61–90 dias vencidos", "> 90 dias vencidos", "Sem vencimento"]
    acc = {name: {"count": 0, "value": 0.0} for name in names}
    today = date.today()
    for due_raw, balance in rows:
        if not due_raw:
            bucket = "Sem vencimento"
        else:
            try:
                due = datetime.fromisoformat(str(due_raw)[:10]).date()
                delta = (today - due).days
                if delta < 0:
                    bucket = "A vencer"
                elif delta == 0:
                    bucket = "Vence hoje"
                elif delta <= 30:
                    bucket = "1–30 dias vencidos"
                elif delta <= 60:
                    bucket = "31–60 dias vencidos"
                elif delta <= 90:
                    bucket = "61–90 dias vencidos"
                else:
                    bucket = "> 90 dias vencidos"
            except Exception:
                bucket = "Sem vencimento"
        acc[bucket]["count"] += 1
        acc[bucket]["value"] += _money(balance)
    return [{"name": name, "count": acc[name]["count"], "value": round(acc[name]["value"], 2)} for name in names if acc[name]["count"]]


def _supplier_breakdown(db, cid: int) -> list[dict]:
    return [dict(r, count=None) for r in _top_open_partners(db, cid, "PAGAR", 100)]


def _partner_map(db, ids: set[int]) -> dict[int, Partner]:
    if not ids:
        return {}
    return {int(p.id): p for p in db.scalars(select(Partner).where(Partner.id.in_(list(ids)))).all()}


def _sales_client_breakdown(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    for inv in sales:
        p = partners.get(int(inv.customer_id or 0))
        name = (p.trade_name or p.name) if p else "Sem cliente identificado"
        cell = acc.setdefault(str(name), {"name": str(name), "count": 0, "value": 0.0})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _sales_region_breakdown(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return []
    ids = [int(r.id) for r in sales]
    delivery_rows = db.execute(select(Delivery.sales_invoice_id, Delivery.region).where(
        Delivery.company_id == cid, Delivery.sales_invoice_id.in_(ids)
    )).all()
    region_by_invoice = {int(invoice_id): str(region or "").strip() for invoice_id, region in delivery_rows}
    acc: dict[str, dict] = {}
    for inv in sales:
        name = region_by_invoice.get(int(inv.id)) or "Sem região classificada"
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _sales_terms_breakdown(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    for inv in sales:
        p = partners.get(int(inv.customer_id or 0))
        name = str((p.payment_terms if p else None) or "Sem prazo cadastrado").strip()
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _seller_breakdown(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return []
    invoice_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in sales if r.raw_record_id])
    order_rows = db.execute(select(
        SalesOrder.erpflex_id, SalesOrder.number, SalesOrder.seller_id, SalesOrder.seller_name, SalesOrder.raw_record_id
    ).where(SalesOrder.company_id == cid)).all()
    order_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in order_rows if r.raw_record_id])
    by_id: dict[str, str] = {}
    by_number: dict[str, list[str]] = defaultdict(list)
    for order in order_rows:
        info = _seller_info(order_payloads.get(int(order.raw_record_id), {}) if order.raw_record_id else {})
        seller = str(order.seller_name or info.get("name") or (f"Vendedor ID {order.seller_id or info.get('id')}" if (order.seller_id or info.get("id")) else "")).strip()
        if not seller:
            continue
        oid = str(order.erpflex_id or "").strip()
        onum = str(order.number or "").strip()
        if oid:
            by_id[oid] = seller
        if onum and seller not in by_number[onum]:
            by_number[onum].append(seller)

    def seller_for(inv) -> str:
        payload = invoice_payloads.get(int(inv.raw_record_id), {}) if inv.raw_record_id else {}
        direct = _seller_info(payload).get("display")
        if direct:
            return str(direct)
        oid = str(_walk_find(payload, {"orcamento_id", "id_orcamento", "pedido_id", "id_pedido", "solicitacao_id", "id_solicitacao"}) or "").strip()
        if oid and oid in by_id:
            return by_id[oid]
        onum = str(_walk_find(payload, {"numero_pedido", "pedido_numero", "numero_orcamento", "documento"}) or "").strip()
        candidates = by_number.get(onum, []) if onum else []
        return candidates[0] if len(candidates) == 1 else "Sem vendedor identificado"

    acc: dict[str, dict] = {}
    for inv in sales:
        name = seller_for(inv)
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _product_breakdown(db, cid: int, start: date, end: date, mode: str) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    invoice_ids = [int(r.id) for r in sales]
    if not invoice_ids:
        return []
    items = db.execute(select(
        SalesInvoiceItem.product_id, SalesInvoiceItem.product_code, SalesInvoiceItem.description,
        SalesInvoiceItem.total, SalesInvoiceItem.sales_invoice_id,
    ).where(SalesInvoiceItem.sales_invoice_id.in_(invoice_ids))).all()
    product_ids = {str(r.product_id or "").strip() for r in items if str(r.product_id or "").strip()}
    codes = {str(r.product_code or "").strip() for r in items if str(r.product_code or "").strip()}
    clauses = []
    if product_ids:
        clauses.append(Product.erpflex_id.in_(list(product_ids)))
    if codes:
        clauses.append(Product.code.in_(list(codes)))
    products = []
    if clauses:
        products = db.scalars(select(Product).where(Product.company_id == cid, or_(*clauses))).all()
    by_external = {str(p.erpflex_id or "").strip(): p for p in products if str(p.erpflex_id or "").strip()}
    by_code = {str(p.code or "").strip(): p for p in products if str(p.code or "").strip()}
    acc: dict[str, dict] = {}
    for item in items:
        product = by_external.get(str(item.product_id or "").strip()) or by_code.get(str(item.product_code or "").strip())
        category = str((product.category if product else None) or "Sem categoria").strip()
        subcategory = str((product.subcategory if product else None) or "Sem subcategoria").strip()
        name = category if mode == "category" else f"{category} › {subcategory}"
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0})
        cell["count"] += 1
        cell["value"] += _money(item.total)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _sales_invoice_detail(db, cid: int, start: date, end: date) -> dict:
    rows = _sales_invoice_rows(db, cid, start, end)[:100]
    return {"title": "NF-e do período", "kind": "sales", "rows": rows}




def _financial_partner_key(row: dict) -> tuple[str, str]:
    if row.get("identity_key"):
        return str(row.get("identity_key")), str(row.get("identity_subtitle") or "")
    cnpj = re.sub(r"\D", "", str(row.get("partner_cnpj") or ""))
    if cnpj:
        return f"tax:{cnpj}", f"CNPJ/CPF {_format_cnpj_cpf(cnpj)}"
    erp = str(row.get("partner_erpflex_id") or "").strip()
    if erp:
        return f"erp:{erp}", f"Cadastro ERP {erp}"
    pid = row.get("partner_id")
    if pid not in (None, ""):
        return f"id:{pid}", f"Cadastro {pid}"
    name = str(row.get("partner") or "Sem cadastro")
    return f"name:{_norm_key(name)}", "Sem CNPJ/CPF identificado"


def _grouped_financial_detail(title: str, rows: list[dict], note: str = "", aggregate_on: str = "balance", partner_label: str = "Cliente/Fornecedor") -> dict:
    acc: dict[str, dict] = {}
    for row in rows:
        key, identity = _financial_partner_key(row)
        name = str(row.get("partner") or "Sem cadastro")
        cell = acc.setdefault(key, {"name": name, "subtitle": identity, "count": 0, "value": 0.0, "titles": []})
        cell["count"] += 1
        cell["value"] += _money(row.get(aggregate_on))
        cell["titles"].append(row)
    all_items = sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)
    total = round(sum(_money(r["value"]) for r in all_items), 2)
    items = all_items[:60]
    for item in items:
        item["share"] = _pct(_money(item["value"]), total) if total else 0.0
        item["titles"] = sorted(item["titles"], key=lambda r: str(r.get("due") or ""), reverse=True)[:60]
    note_extra = f" Exibindo os {len(items)} maiores grupos." if len(all_items) > len(items) else ""
    return {"title": title, "kind": "grouped_financial", "rows": items, "total": total, "note": (note + note_extra).strip(), "partner_label": partner_label}


def _grouped_contract_detail(title: str, rows: list[dict], note: str = "") -> dict:
    acc: dict[str, dict] = {}
    for row in rows:
        key, identity = _financial_partner_key(row)
        name = str(row.get("partner") or "Sem cadastro")
        cell = acc.setdefault(key, {"name": name, "subtitle": identity, "count": 0, "value": 0.0, "gross": 0.0, "discount": 0.0, "received": 0.0, "titles": []})
        cell["count"] += 1
        cell["value"] += _money(row.get("net_balance"))
        cell["gross"] += _money(row.get("value"))
        cell["discount"] += _money(row.get("discount"))
        cell["received"] += _money(row.get("paid"))
        cell["titles"].append(row)
    all_items = sorted(({**v, "value": round(v["value"], 2), "gross": round(v["gross"], 2), "discount": round(v["discount"], 2), "received": round(v["received"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)
    total = round(sum(_money(r["value"]) for r in all_items), 2)
    items = all_items[:60]
    for item in items:
        item["share"] = _pct(_money(item["value"]), total) if total else 0.0
        item["titles"] = item["titles"][:60]
    note_extra = f" Exibindo os {len(items)} maiores clientes." if len(all_items) > len(items) else ""
    return {"title": title, "kind": "grouped_contract", "rows": items, "total": total, "note": (note + note_extra).strip(), "partner_label": "Cliente"}


def _group_breakdown_with_children(title: str, rows: list[dict], note: str = "") -> dict:
    total = round(sum(_money(r.get("value")) for r in rows), 2)
    all_count = len(rows)
    rows = rows[:60]
    for row in rows:
        row["share"] = _pct(_money(row.get("value")), total) if total else 0.0
        if "invoices" in row:
            row["invoices"] = (row.get("invoices") or [])[:60]
        if "products" in row:
            row["products"] = (row.get("products") or [])[:60]
    note_extra = f" Exibindo os {len(rows)} maiores grupos." if all_count > len(rows) else ""
    return {"title": title, "kind": "grouped_breakdown", "rows": rows, "total": total, "note": (note + note_extra).strip()}


def _format_cnpj_cpf(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return str(value or "").strip()


def _partner_identity(partner: Partner | None) -> tuple[str, str, str]:
    if not partner:
        return "missing", "Sem cadastro", ""
    name = str(partner.trade_name or partner.name or "Sem cadastro").strip() or "Sem cadastro"
    tax_raw = str(partner.cnpj_cpf or "").strip()
    tax_digits = re.sub(r"\D", "", tax_raw)
    if tax_digits:
        return f"tax:{tax_digits}", name, f"CNPJ/CPF {_format_cnpj_cpf(tax_raw)}"
    erp = str(partner.erpflex_id or "").strip()
    if erp:
        return f"erp:{erp}", name, f"Cadastro ERP {erp}"
    return f"id:{int(partner.id)}", name, f"Cadastro {int(partner.id)}"


def _classify_zone(name: str, city: str = "", district: str = "", zip_code: str = "", state: str = "") -> str:
    """Consolida as regiões operacionais nas faixas comerciais pedidas no BI.

    A região logística explícita tem prioridade. Quando ela é genérica (A Classificar,
    Outras Regiões etc.), reaproveitamos o classificador do roteirizador com cidade,
    bairro e CEP da entrega.
    """
    def norm(v: Any) -> str:
        return unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode("ascii").lower().strip()

    raw = norm(name)
    explicit = (
        ("zona sul", "Zona Sul"), ("zona leste", "Zona Leste"),
        ("zona norte", "Zona Norte"), ("zona oeste", "Zona Oeste"),
        ("abc", "ABC"), ("alto tiete", "Alto Tietê"),
        ("interior", "Interior"),
    )
    for token, label in explicit:
        if token in raw:
            return label
    if "osasco" in raw or "barueri" in raw:
        return "Zona Oeste"

    inferred = classificar_regiao(city, district, zip_code, "") if (city or district or zip_code) else ""
    inf = norm(inferred)
    for token, label in explicit:
        if token in inf:
            return label
    if "osasco" in inf or "barueri" in inf:
        return "Zona Oeste"

    # Fora das zonas comerciais definidas pelo usuário: Centro, Guarulhos, Litoral,
    # coleta, registros sem classificação e UFs fora de SP ficam consolidados em Outros.
    st = norm(state).upper()
    if st and st not in {"SP", "SAO PAULO"}:
        return "Outros"
    return "Outros"


def _sales_invoice_rows(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    rows = []
    for r in sorted(sales, key=lambda x: str(x.issue_date or ""), reverse=True):
        partner = partners.get(int(r.customer_id or 0))
        pkey, pname, psubtitle = _partner_identity(partner)
        rows.append({
            "id": int(r.id), "number": r.number or "—", "date": r.issue_date or "—",
            "partner": pname, "partner_key": pkey, "partner_subtitle": psubtitle,
            "value": _money(r.total),
        })
    return rows


def _sales_grouped_by_partner(db, cid: int, start: date, end: date) -> list[dict]:
    rows = _sales_invoice_rows(db, cid, start, end)
    acc: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("partner_key") or row.get("partner") or "Sem cadastro")
        cell = acc.setdefault(key, {
            "name": row.get("partner") or "Sem cadastro",
            "subtitle": row.get("partner_subtitle") or "",
            "count": 0, "value": 0.0, "invoices": []
        })
        cell["count"] += 1
        cell["value"] += _money(row["value"])
        cell["invoices"].append(row)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _sales_grouped_by_zone(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return []
    ids = [int(r.id) for r in sales]
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    delivery_rows = db.execute(select(Delivery.sales_invoice_id, Delivery.region).where(
        Delivery.company_id == cid, Delivery.sales_invoice_id.in_(ids)
    )).all()
    region_by_invoice = {int(invoice_id): str(region or "").strip() for invoice_id, region in delivery_rows}
    acc: dict[str, dict] = {}
    for inv in sales:
        partner = partners.get(int(inv.customer_id or 0))
        raw_region = region_by_invoice.get(int(inv.id)) or ""
        city = str(inv.delivery_city or (partner.delivery_city if partner else "") or (partner.city if partner else "") or "").strip()
        district = str(inv.delivery_district or (partner.delivery_district if partner else "") or (partner.district if partner else "") or "").strip()
        zip_code = str(inv.delivery_zip or (partner.delivery_zip_code if partner else "") or (partner.zip_code if partner else "") or "").strip()
        state = str(inv.delivery_state or (partner.delivery_state if partner else "") or (partner.state if partner else "") or "").strip()
        zone = _classify_zone(raw_region, city, district, zip_code, state)
        _, pname, _ = _partner_identity(partner)
        invoice = {
            "id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—",
            "partner": pname, "value": _money(inv.total), "region": raw_region or "Não informada",
            "location": " / ".join(x for x in (district, city, state) if x),
        }
        cell = acc.setdefault(zone, {"name": zone, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
    preferred = {name: i for i, name in enumerate(("Zona Sul", "Zona Leste", "Zona Norte", "Zona Oeste", "ABC", "Alto Tietê", "Interior", "Outros"))}
    result = [{**v, "value": round(v["value"], 2)} for v in acc.values()]
    return sorted(result, key=lambda r: (preferred.get(r["name"], 99), -r["value"]))


def _sales_grouped_by_state(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return []
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    for inv in sales:
        partner = partners.get(int(inv.customer_id or 0))
        state = str((getattr(inv, "delivery_state", None) or (partner.delivery_state if partner and partner.delivery_state else partner.state if partner else None) or "Sem estado")).strip().upper()
        invoice = {"id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—", "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro", "value": _money(inv.total)}
        cell = acc.setdefault(state, {"name": state, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)



def _norm_key(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


def _parse_calendar_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    for candidate in (s[:10], s):
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except Exception:
            pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    return None


def _iter_payload_values(obj: Any, wanted: set[str]):
    if isinstance(obj, dict):
        for key, value in obj.items():
            nk = _norm_key(key)
            if nk in wanted:
                yield value
            yield from _iter_payload_values(value, wanted)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_payload_values(value, wanted)


def _label_from_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        for key in ("descricao", "description", "nome", "name", "label", "prazo", "condicao"):
            if value.get(key) not in (None, ""):
                return str(value.get(key)).strip()
        return ""
    if isinstance(value, list):
        labels = [_label_from_value(x) for x in value]
        labels = [x for x in labels if x]
        return " / ".join(dict.fromkeys(labels))
    if isinstance(value, (int, float)):
        return f"{int(value) if float(value).is_integer() else value} dias"
    return str(value).strip()


def _invoice_payment_payloads(db, cid: int, sales: list) -> dict[int, dict]:
    header_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in sales if r.raw_record_id])
    ext_ids = [str(r.erpflex_id) for r in sales if getattr(r, "erpflex_id", None)]
    detail_by_ext: dict[str, dict] = {}
    if ext_ids:
        raws = db.execute(select(RawRecord.external_id, RawRecord.payload_json).where(
            RawRecord.company_id == cid,
            func.upper(RawRecord.source) == "ERPFLEX",
            func.lower(RawRecord.module) == "faturamento_detalhe",
            RawRecord.external_id.in_(ext_ids),
        )).all()
        for external_id, payload_json in raws:
            try:
                payload = json.loads(payload_json or "{}")
                if isinstance(payload, dict):
                    detail_by_ext[str(external_id)] = payload
            except Exception:
                continue
    out: dict[int, dict] = {}
    for inv in sales:
        header = header_payloads.get(int(inv.raw_record_id), {}) if inv.raw_record_id else {}
        detail = detail_by_ext.get(str(getattr(inv, "erpflex_id", "") or ""), {})
        # O detalhe tem prioridade, mas o cabeçalho pode conter a condição do pedido.
        merged = dict(header) if isinstance(header, dict) else {}
        if isinstance(detail, dict):
            merged["__detail__"] = detail
        out[int(inv.id)] = merged
    return out


def _payment_label_from_due_dates(issue_raw: Any, due_dates: list[date], source: str) -> tuple[str, str]:
    issue = _parse_calendar_date(issue_raw)
    if not issue or not due_dates:
        return "Prazo não identificado", "indisponível"
    offsets = sorted({(d - issue).days for d in due_dates if d >= issue})
    if not offsets:
        return "Prazo não identificado", "indisponível"
    if offsets == [0]:
        return "À vista", source
    positives = [x for x in offsets if x > 0]
    if positives:
        return "/".join(str(x) for x in positives) + " dias", source
    return "À vista", source


def _payment_term_from_payload(payload: dict, issue_raw: Any) -> tuple[str, str]:
    # Primeiro usa vencimentos/duplicatas da própria cobrança da NF/pedido: eles representam
    # melhor o prazo efetivamente praticado do que o texto cadastral da condição.
    detail = payload.get("__detail__") if isinstance(payload, dict) else None
    sources = [detail, payload] if isinstance(detail, dict) else [payload]
    due_keys = {"vencimento", "data_vencimento", "dt_vencimento", "due_date", "data_vcto", "vcto"}
    due_dates: list[date] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for value in _iter_payload_values(source, due_keys):
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate = candidate.get("data") or candidate.get("vencimento") or candidate.get("due_date")
                d = _parse_calendar_date(candidate)
                if d:
                    due_dates.append(d)
    label, origin = _payment_label_from_due_dates(issue_raw, due_dates, "cobrança da NF/pedido")
    if label != "Prazo não identificado":
        return label, origin

    term_keys = {
        "prazo_pagamento", "prazo_de_pagamento", "condicao_pagamento", "condicao_de_pagamento",
        "payment_terms", "payment_term", "descricao_condicao_pagamento", "forma_prazo_pagamento",
        "condicao_pgto", "prazo_pgto", "cond_pagamento"
    }
    for source in sources:
        if not isinstance(source, dict):
            continue
        for value in _iter_payload_values(source, term_keys):
            text_label = _label_from_value(value)
            if text_label and text_label not in {"0", "0 dias"}:
                if re.fullmatch(r"\d+(?:[.,]\d+)?", text_label):
                    text_label = f"{text_label} dias"
                return text_label, "condição da NF/pedido"
    return "Prazo não identificado", "indisponível"


def _doc_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()
    if not text:
        return ""
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def _payload_ref_tokens(payload: dict, keys: set[str]) -> set[str]:
    out: set[str] = set()
    if not isinstance(payload, dict):
        return out
    for value in _iter_payload_values(payload, keys):
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict):
                for k in ("numero", "documento", "id", "nfe", "pedido"):
                    if candidate.get(k) not in (None, ""):
                        tok = _doc_token(candidate.get(k))
                        if tok:
                            out.add(tok)
            else:
                tok = _doc_token(candidate)
                if tok:
                    out.add(tok)
    return out


def _receivable_terms_context(db, cid: int, sales: list, start: date, end: date):
    partner_ids = {int(r.customer_id) for r in sales if r.customer_id}
    if not partner_ids:
        return lambda inv, payload: ("Prazo não identificado", "indisponível")

    pad_start = (start - timedelta(days=7)).isoformat()
    pad_end = (end + timedelta(days=7)).isoformat()
    titles = db.execute(select(
        FinancialTitle.id, FinancialTitle.partner_id, FinancialTitle.document, FinancialTitle.issue_date,
        FinancialTitle.due_date, FinancialTitle.value, FinancialTitle.raw_record_id
    ).where(
        FinancialTitle.company_id == cid, FinancialTitle.kind == "RECEBER",
        FinancialTitle.partner_id.in_(list(partner_ids)),
        or_(FinancialTitle.issue_date == None, and_(FinancialTitle.issue_date >= pad_start, FinancialTitle.issue_date <= pad_end)),
    )).all()
    raw_map = _load_raw_payloads(db, [int(t.raw_record_id) for t in titles if t.raw_record_id])

    note_keys = {"nr_nfe", "nfe", "numero_nfe", "numero_nf", "numero_nota", "nota_fiscal", "sf2_nrnfe"}
    commercial_keys = {"documento", "numero_pedido", "pedido", "pedido_numero", "numero_orcamento", "orcamento", "titulo"}
    by_note: dict[tuple[int, str], list] = defaultdict(list)
    by_doc: dict[tuple[int, str], list] = defaultdict(list)
    by_issue: dict[tuple[int, str], list] = defaultdict(list)
    for t in titles:
        pid = int(t.partner_id or 0)
        payload = raw_map.get(int(t.raw_record_id), {}) if t.raw_record_id else {}
        for tok in _payload_ref_tokens(payload, note_keys):
            by_note[(pid, tok)].append(t)
        doc_tokens = {_doc_token(t.document)} if t.document else set()
        doc_tokens |= _payload_ref_tokens(payload, commercial_keys)
        for tok in {x for x in doc_tokens if x}:
            by_doc[(pid, tok)].append(t)
        if t.issue_date:
            by_issue[(pid, str(t.issue_date)[:10])].append(t)

    invoice_counts: dict[tuple[int, str], int] = defaultdict(int)
    for inv in sales:
        if inv.customer_id and inv.issue_date:
            invoice_counts[(int(inv.customer_id), str(inv.issue_date)[:10])] += 1

    def term_for(inv, payload: dict) -> tuple[str, str]:
        direct = _payment_term_from_payload(payload, inv.issue_date)
        if direct[0] != "Prazo não identificado":
            return direct
        pid = int(inv.customer_id or 0)
        candidates = []
        origin = ""
        nf_token = _doc_token(inv.number)
        if nf_token:
            candidates = list(by_note.get((pid, nf_token), []))
            if candidates:
                origin = "Contas a Receber · NF-e"
        if not candidates:
            commercial_refs = _payload_ref_tokens(payload, {"documento", "numero_pedido", "pedido_numero", "pedido", "numero_orcamento", "orcamento"})
            matched = []
            for ref in commercial_refs:
                matched.extend(by_doc.get((pid, ref), []))
            if matched:
                candidates = matched
                origin = "Contas a Receber · pedido/documento"
        if not candidates and nf_token:
            candidates = list(by_doc.get((pid, nf_token), []))
            if candidates:
                origin = "Contas a Receber · documento"
        issue_key = (pid, str(inv.issue_date or "")[:10])
        issue_candidates = list(by_issue.get(issue_key, []))
        if not candidates and invoice_counts.get(issue_key, 0) == 1:
            candidates = issue_candidates
            if candidates:
                origin = "Contas a Receber · emissão"
        # Se houver várias NF do mesmo cliente no dia, tenta identificar o único documento
        # financeiro cujas parcelas somam o total da NF. Não usa aproximação ambígua.
        if not candidates and issue_candidates and invoice_counts.get(issue_key, 0) > 1:
            groups: dict[str, list] = defaultdict(list)
            for t in issue_candidates:
                tok = _doc_token(t.document) or f"title:{int(t.id)}"
                groups[tok].append(t)
            invoice_total = _money(getattr(inv, "total", 0))
            tolerance = max(0.05, abs(invoice_total) * 0.001)
            matches = [g for g in groups.values() if abs(sum(_money(x.value) for x in g) - invoice_total) <= tolerance]
            if len(matches) == 1:
                candidates = matches[0]
                origin = "Contas a Receber · emissão/valor"
        seen = set()
        unique = []
        for t in candidates:
            if int(t.id) not in seen:
                seen.add(int(t.id))
                unique.append(t)
        due_dates = [d for d in (_parse_calendar_date(t.due_date) for t in unique) if d]
        return _payment_label_from_due_dates(inv.issue_date, due_dates, origin or "Contas a Receber")

    return term_for


def _sales_grouped_by_practiced_payment(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    payloads = _invoice_payment_payloads(db, cid, sales)
    receivable_term_for = _receivable_terms_context(db, cid, sales, start, end)
    acc: dict[str, dict] = {}
    for inv in sales:
        payload = payloads.get(int(inv.id), {})
        name, source = receivable_term_for(inv, payload)
        partner = partners.get(int(inv.customer_id or 0))
        _, pname, _ = _partner_identity(partner)
        invoice = {
            "id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—",
            "partner": pname, "value": _money(inv.total), "payment_source": source,
        }
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "invoices": [], "sources": set()})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
        cell["sources"].add(source)
    result = []
    for cell in acc.values():
        cell["value"] = round(cell["value"], 2)
        cell["subtitle"] = "Origem: " + ", ".join(sorted(cell.pop("sources")))
        result.append(cell)
    return sorted(result, key=lambda r: (r["name"] == "Prazo não identificado", -r["value"]))


def _sales_grouped_by_value_range(db, cid: int, start: date, end: date) -> list[dict]:
    rows = _sales_invoice_rows(db, cid, start, end)
    buckets = [
        ("Até R$ 500", 0.0, 500.0),
        ("R$ 500,01 a R$ 1.000", 500.0, 1000.0),
        ("R$ 1.000,01 a R$ 3.000", 1000.0, 3000.0),
        ("R$ 3.000,01 a R$ 5.000", 3000.0, 5000.0),
        ("R$ 5.000,01 a R$ 10.000", 5000.0, 10000.0),
        ("Acima de R$ 10.000", 10000.0, None),
    ]
    acc = {name: {"name": name, "count": 0, "value": 0.0, "invoices": []} for name, _, _ in buckets}
    for row in rows:
        value = _money(row.get("value"))
        chosen = buckets[-1][0]
        for name, low, high in buckets:
            if high is None:
                if value > low:
                    chosen = name
                    break
            elif value > low and value <= high:
                chosen = name
                break
            elif low == 0 and 0 <= value <= high:
                chosen = name
                break
        cell = acc[chosen]
        cell["count"] += 1
        cell["value"] += value
        cell["invoices"].append(row)
    result = []
    for name, _, _ in buckets:
        cell = acc[name]
        if not cell["count"]:
            continue
        cell["value"] = round(cell["value"], 2)
        cell["avg_ticket"] = round(cell["value"] / cell["count"], 2) if cell["count"] else 0.0
        cell["subtitle"] = f"Ticket médio: R$ {cell['avg_ticket']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        result.append(cell)
    return result



PRICE_TABLE_LABELS = {
    0: "Tabela Padrão",
    1: "CIF",
    2: "Contrato 10%",
    3: "Natural do libano",
    4: "Casa Santa Luiza",
    5: "Carrefour",
    6: "Zaffari 14%",
    7: "WalMart RS e PR",
    8: "WalMart SP e MG",
    9: "Mambo",
    10: "Pão de Açucar",
    11: "Lebanon",
    12: "Imigrantes",
    13: "St Marche",
    14: "11%",
    15: "15%",
    16: "Confianca 13%",
    17: "PegPese 5% + torrada",
    18: "Comper",
    19: "Pastorinho",
    20: "Rede Oba",
    21: "Hirota 10%",
    22: "5%",
    23: "6%",
    24: "7%",
    25: "Clube Monte Libano",
    26: "8%",
}


def _price_table_label(raw_name: Any) -> tuple[str, str | None]:
    raw = str(raw_name or "").strip()
    if not raw:
        return "", None
    norm = _norm_key(raw)
    if norm in {"tabelas", "tabela", "prices", "precos"}:
        return "", None
    idx = None
    for prefix in ("tabela_", "tabela", "table_", "table"):
        if norm.startswith(prefix):
            tail = norm[len(prefix):].strip("_- ")
            if tail.isdigit():
                idx = int(tail)
                break
    if idx is None and norm.isdigit():
        idx = int(norm)
    if idx is not None and idx in PRICE_TABLE_LABELS:
        technical = "Padrão" if idx == 0 else f"Tabela {idx}"
        return PRICE_TABLE_LABELS[idx], technical
    if norm in {"padrao", "padrão", "tabela_padrao", "tabela_padrão"}:
        return PRICE_TABLE_LABELS[0], "Padrão"
    return raw, None

def _price_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if not isinstance(value, str):
        return None
    s = value.strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 4)
    except Exception:
        return None


def _extract_price_tables(payload: dict) -> list[dict]:
    found: dict[tuple[str, float], dict] = {}
    table_name_keys = {"tabela", "tabela_preco", "nome_tabela", "descricao_tabela", "lista", "lista_preco", "nome_lista", "descricao_lista"}
    price_keys = {"preco", "preco_venda", "valor", "valor_venda", "price", "preco_tabela", "valor_tabela", "vl_preco"}
    container_keys = {"tabelas_preco", "tabela_preco", "precos_tabela", "listas_preco", "lista_preco", "precos", "prices"}

    def add(name: Any, price: Any, source: str):
        num = _price_number(price)
        label, technical = _price_table_label(name)
        if num is None or num < 0 or not label:
            return
        ref = _canonical_price_table_ref(name)
        table_index = ref.get("index") if ref else None
        found[(label, num)] = {"table": label, "table_code": technical, "table_index": table_index, "price": num, "source": source}

    def walk(obj: Any, context: str = ""):
        if isinstance(obj, dict):
            normalized = {_norm_key(k): v for k, v in obj.items()}
            names = [normalized.get(k) for k in table_name_keys if normalized.get(k) not in (None, "")]
            prices = [(k, normalized.get(k)) for k in price_keys if normalized.get(k) not in (None, "")]
            if names and prices:
                for pk, price in prices:
                    add(_label_from_value(names[0]), price, pk)
            for key, value in obj.items():
                nk = _norm_key(key)
                if nk in container_keys:
                    if isinstance(value, dict):
                        for subkey, subval in value.items():
                            if _price_number(subval) is not None:
                                add(subkey, subval, nk)
                            elif isinstance(subval, dict):
                                walk(subval, str(subkey))
                    elif isinstance(value, list):
                        for entry in value:
                            walk(entry, nk)
                walk(value, context or nk)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, context)

    walk(payload)
    # Quando a API só traz um preço de venda único, manter separado de tabela nominal.
    root = {_norm_key(k): v for k, v in payload.items()} if isinstance(payload, dict) else {}
    for key in ("preco_venda", "valor_venda"):
        if key in root and _price_number(root[key]) is not None:
            add("Preço de venda / API", root[key], key)
            break
    return sorted(found.values(), key=lambda r: (r["table"].lower(), r["price"]))


def _canonical_price_table_ref(value: Any) -> dict | None:
    """Normaliza a referência de tabela encontrada em NF/pedido para o mapa conhecido."""
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        norm = {_norm_key(k): v for k, v in value.items()}
        for key in ("nome_tabela", "descricao_tabela", "descricao", "nome", "label", "legenda"):
            if norm.get(key) not in (None, ""):
                result = _canonical_price_table_ref(norm.get(key))
                if result:
                    # completar o índice quando o objeto também o trouxer
                    for ikey in ("id_tabela_preco", "tabela_preco_id", "id", "codigo", "numero", "tabela"):
                        if norm.get(ikey) not in (None, ""):
                            idx_ref = _canonical_price_table_ref(norm.get(ikey))
                            if idx_ref and idx_ref.get("index") is not None:
                                result["index"] = idx_ref["index"]
                                result["technical"] = idx_ref.get("technical")
                                break
                    return result
        for key in ("id_tabela_preco", "tabela_preco_id", "codigo_tabela_preco", "cod_tabela_preco", "id", "codigo", "numero", "tabela"):
            if norm.get(key) not in (None, ""):
                result = _canonical_price_table_ref(norm.get(key))
                if result:
                    return result
        return None
    if isinstance(value, (int, float)):
        try:
            idx = int(value)
        except Exception:
            return None
        if idx in PRICE_TABLE_LABELS:
            return {"index": idx, "label": PRICE_TABLE_LABELS[idx], "technical": "Padrão" if idx == 0 else f"Tabela {idx}", "raw": str(value)}
        return {"index": idx, "label": f"Tabela {idx}", "technical": f"Tabela {idx}", "raw": str(value)}
    raw = str(value or "").strip()
    if not raw:
        return None
    norm = _norm_key(raw)
    # descrição conhecida
    for idx, label in PRICE_TABLE_LABELS.items():
        if _norm_key(label) == norm:
            return {"index": idx, "label": label, "technical": "Padrão" if idx == 0 else f"Tabela {idx}", "raw": raw}
    # formatos tabela_10 / Tabela 10 / Padrão. Para texto livre, continua
    # abaixo para tentar extrair um índice embutido antes de assumir o rótulo cru.
    label, technical = _price_table_label(raw)
    if technical:
        idx = None
        if technical == "Padrão":
            idx = 0
        elif re.search(r"(\d+)$", technical):
            idx = int(re.search(r"(\d+)$", technical).group(1))
        return {"index": idx, "label": label, "technical": technical, "raw": raw}
    digits = re.fullmatch(r"0*(\d+)", norm)
    if digits:
        idx = int(digits.group(1))
        return {"index": idx, "label": PRICE_TABLE_LABELS.get(idx, f"Tabela {idx}"), "technical": "Padrão" if idx == 0 else f"Tabela {idx}", "raw": raw}
    # Formatos comuns do ERP: "10 - Pão de Açucar", "Tabela 10 - ...", "10|...".
    mixed = re.search(r"(?:^|tabela[_\s-]*)(\d{1,2})(?:\D|$)", norm)
    if mixed:
        idx = int(mixed.group(1))
        if idx in PRICE_TABLE_LABELS:
            return {"index": idx, "label": PRICE_TABLE_LABELS[idx], "technical": "Padrão" if idx == 0 else f"Tabela {idx}", "raw": raw}
    return {"index": None, "label": raw, "technical": None, "raw": raw}


def _extract_sale_price_table(payload: dict | None) -> dict | None:
    """Localiza a tabela de preço efetivamente indicada na venda.

    Prioriza campos explícitos de tabela/lista de preço e evita interpretar o
    contêiner genérico ``tabelas`` como uma referência de venda.
    """
    if not isinstance(payload, dict):
        return None
    exact_priority = {
        "tabela_preco": 100, "tabela_de_preco": 100, "tabelapreco": 100,
        "price_table": 100, "price_list": 100, "pricelist": 100,
        "nome_tabela_preco": 98, "descricao_tabela_preco": 98, "tabela_preco_desc": 98, "tabelapreco_desc": 98,
        "id_tabela_preco": 99, "tabela_preco_id": 99, "idtabelapreco": 99,
        "id_tabela": 99, "tabela_id": 99, "idtabela": 99,
        "codigo_tabela_preco": 98, "cod_tabela_preco": 98, "numero_tabela_preco": 98, "nr_tabela_preco": 98,
        "codigo_tabela": 98, "cod_tabela": 98, "numero_tabela": 98, "nr_tabela": 98,
        "lista_preco": 92, "lista_de_preco": 92, "id_lista_preco": 90, "lista_preco_id": 90,
        "tabela_venda": 90, "tabela_comercial": 90, "tab_preco": 90, "tabpreco": 90,
    }
    candidates: list[tuple[int, int, dict]] = []

    def walk(obj: Any, depth: int = 0):
        if depth > 8:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                nk = _norm_key(key)
                if nk in exact_priority and value not in (None, "", [], {}):
                    ref = _canonical_price_table_ref(value)
                    if ref:
                        candidates.append((exact_priority[nk], -depth, {**ref, "source": nk}))
                # Alguns retornos usam somente `tabela`; aceitar escalar ou objeto
                # identificável, mas nunca o contêiner genérico de tabelas do produto.
                elif nk == "tabela" and not isinstance(value, list) and value not in (None, "", {}):
                    ref = _canonical_price_table_ref(value)
                    if ref and (_norm_key(str(value)) not in {"tabelas", "tabela"}):
                        candidates.append((72, -depth, {**ref, "source": nk}))
                # Variações não documentadas: qualquer campo escalar cujo nome contenha
                # simultaneamente tabela/lista e preço, sem interpretar arrays de preços.
                elif not isinstance(value, (dict, list)) and value not in (None, "") and (("tabela" in nk and "preco" in nk) or ("lista" in nk and "preco" in nk)):
                    ref = _canonical_price_table_ref(value)
                    if ref:
                        candidates.append((80, -depth, {**ref, "source": nk}))
                walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, depth + 1)
    walk(payload)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def _product_price_reference_context(db, cid: int, item_rows: list) -> Any:
    """Retorna função que resolve produto + preços de tabela para itens faturados.

    Usa ID, código e EAN; em seguida usa descrição apenas quando o cadastro é único.
    Isso evita que a auditoria perca referência quando a NF não grava o mesmo ID usado
    no cadastro mestre do produto.
    """
    product_ids = {str(r.product_id or "").strip() for r in item_rows if str(r.product_id or "").strip()}
    codes = {str(r.product_code or "").strip() for r in item_rows if str(r.product_code or "").strip()}
    eans = {re.sub(r"\D", "", str(r.ean or "")) for r in item_rows if re.sub(r"\D", "", str(r.ean or ""))}
    descriptions = {_norm_key(str(r.description or "")) for r in item_rows if _norm_key(str(r.description or ""))}

    products_by_id: dict[int, Product] = {}
    # Consultas em blocos para funcionar também no SQLite (limite de parâmetros).
    def collect(field, values):
        vals = list(values)
        for i in range(0, len(vals), 400):
            chunk = vals[i:i+400]
            if not chunk:
                continue
            for prod in db.scalars(select(Product).where(Product.company_id == cid, field.in_(chunk))).all():
                products_by_id[int(prod.id)] = prod
    collect(Product.erpflex_id, product_ids)
    collect(Product.code, codes)
    # EAN é armazenado como texto; consulta exata e complementa com mapa normalizado.
    collect(Product.ean, {str(r.ean or "").strip() for r in item_rows if str(r.ean or "").strip()})
    products = list(products_by_id.values())

    by_external = {str(p.erpflex_id or "").strip(): p for p in products if str(p.erpflex_id or "").strip()}
    by_code = {_norm_key(str(p.code or "")): p for p in products if _norm_key(str(p.code or ""))}
    by_ean = {re.sub(r"\D", "", str(p.ean or "")): p for p in products if re.sub(r"\D", "", str(p.ean or ""))}
    desc_candidates: dict[str, list[Product]] = defaultdict(list)
    for prod in products:
        if prod.description:
            desc_candidates[_norm_key(prod.description)].append(prod)
    by_desc = {k: vals[0] for k, vals in desc_candidates.items() if len(vals) == 1 and k in descriptions}

    ext_ids = {str(p.erpflex_id or "").strip() for p in products if str(p.erpflex_id or "").strip()} | product_ids
    raw_by_external: dict[str, dict] = {}
    raw_by_code: dict[str, dict] = {}
    raw_by_ean: dict[str, dict] = {}
    ext_list = list(ext_ids)
    for i in range(0, len(ext_list), 400):
        chunk = ext_list[i:i+400]
        if not chunk:
            continue
        raw_rows = db.execute(select(RawRecord.external_id, RawRecord.payload_json).where(
            RawRecord.company_id == cid,
            func.upper(RawRecord.source) == "ERPFLEX",
            func.lower(RawRecord.module) == "products",
            RawRecord.external_id.in_(chunk),
        ).order_by(RawRecord.id.desc())).all()
        for external_id, payload_json in raw_rows:
            ext = str(external_id or "").strip()
            if ext in raw_by_external:
                continue
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if ext:
                raw_by_external[ext] = payload
            raw_code = _walk_find(payload, {"codigo", "codigo_produto", "sku", "referencia"})
            if raw_code not in (None, ""):
                raw_by_code[_norm_key(str(raw_code))] = payload
            raw_ean = _walk_find(payload, {"ean", "gtin", "codigo_barras", "cean"})
            raw_ean_digits = re.sub(r"\D", "", str(raw_ean or ""))
            if raw_ean_digits:
                raw_by_ean[raw_ean_digits] = payload

    def resolve(item) -> dict:
        pid = str(item.product_id or "").strip()
        code = _norm_key(str(item.product_code or ""))
        ean = re.sub(r"\D", "", str(item.ean or ""))
        desc = _norm_key(str(item.description or ""))
        product = by_code.get(code) or by_external.get(pid) or by_ean.get(ean) or by_desc.get(desc)
        match_method = ""
        if code and by_code.get(code):
            match_method = "product_code"
        elif pid and by_external.get(pid):
            match_method = "product_id"
        elif ean and by_ean.get(ean):
            match_method = "ean"
        elif desc and by_desc.get(desc):
            match_method = "description"
        payload = raw_by_code.get(code) or raw_by_external.get(pid) or raw_by_ean.get(ean)
        if not payload and product:
            payload = raw_by_external.get(str(product.erpflex_id or "").strip())
        prices = _extract_price_tables(payload or {}) if payload else []
        return {
            "product": product, "payload": payload or {}, "prices": prices, "matched": bool(product or payload),
            "match_method": match_method, "resolved_code": str((product.code if product else item.product_code) or "").strip(),
        }
    return resolve

def _direct_price_from_product_payload(payload: dict | None, table_ref: dict | None) -> dict | None:
    """Busca o preço pelo ID técnico da tabela diretamente no payload do produto.

    Esta é a rota preferencial da auditoria: ID da tabela da venda + código do produto
    faturado -> payload do mesmo produto -> preço da mesma tabela. O nome/legenda da
    tabela é usado apenas para exibição e fallback.
    """
    if not isinstance(payload, dict) or not table_ref or table_ref.get("index") is None:
        return None
    idx = int(table_ref.get("index"))
    exact_keys = {f"tabela_{idx}", f"tabela{idx}", f"table_{idx}", f"table{idx}"}
    if idx == 0:
        exact_keys |= {"padrao", "padrão", "tabela_padrao", "tabela_padrão", "preco_padrao", "preço_padrao"}
    explicit_id_keys = {
        "id_tabela_preco", "tabela_preco_id", "idtabelapreco", "id_tabela", "tabela_id", "idtabela",
        "codigo_tabela_preco", "cod_tabela_preco", "numero_tabela_preco", "nr_tabela_preco",
        "codigo_tabela", "cod_tabela", "numero_tabela", "nr_tabela",
    }
    generic_id_keys = {"codigo", "numero", "id"}
    price_keys = {"preco", "preco_venda", "valor", "valor_venda", "price", "preco_tabela", "valor_tabela", "vl_preco"}

    def scalar_price(value: Any) -> float | None:
        num = _price_number(value)
        return num if num is not None and num >= 0 else None

    def object_price(obj: Any) -> float | None:
        if not isinstance(obj, dict):
            return scalar_price(obj)
        norm = {_norm_key(k): v for k, v in obj.items()}
        for key in price_keys:
            if key in norm:
                num = scalar_price(norm.get(key))
                if num is not None:
                    return num
        return None

    found: list[dict] = []

    def walk(obj: Any, depth: int = 0, path: str = ""):
        if depth > 9:
            return
        if isinstance(obj, dict):
            norm = {_norm_key(k): v for k, v in obj.items()}
            # 1) Campo direto tabela_N no payload do produto.
            for key, value in obj.items():
                nk = _norm_key(key)
                if nk in exact_keys:
                    num = object_price(value)
                    if num is not None:
                        found.append({
                            "table": PRICE_TABLE_LABELS.get(idx, f"Tabela {idx}"),
                            "table_code": "Padrão" if idx == 0 else f"Tabela {idx}",
                            "table_index": idx, "price": num, "source": f"payload:{nk}",
                            "match": "table_id+product_code",
                        })
            # 2) Estrutura de lista/objeto com ID da tabela + preço.
            ids = []
            for key in explicit_id_keys:
                if norm.get(key) not in (None, ""):
                    ref = _canonical_price_table_ref(norm.get(key))
                    if ref and ref.get("index") is not None:
                        ids.append(int(ref.get("index")))
            # Campos genéricos como `id`/`codigo` só são aceitos dentro de uma
            # estrutura claramente relacionada a tabela/lista de preço, evitando
            # confundir o ID ou código do próprio produto com ID de tabela.
            table_context = any(token in _norm_key(path) for token in ("tabela", "preco", "price", "lista"))
            if table_context:
                for key in generic_id_keys:
                    if norm.get(key) not in (None, ""):
                        ref = _canonical_price_table_ref(norm.get(key))
                        if ref and ref.get("index") is not None:
                            ids.append(int(ref.get("index")))
            if idx in ids:
                num = object_price(obj)
                if num is not None:
                    found.append({
                        "table": PRICE_TABLE_LABELS.get(idx, f"Tabela {idx}"),
                        "table_code": "Padrão" if idx == 0 else f"Tabela {idx}",
                        "table_index": idx, "price": num, "source": f"payload:{path or 'objeto_tabela'}",
                        "match": "table_id+product_code",
                    })
            for key, value in obj.items():
                walk(value, depth + 1, f"{path}.{_norm_key(key)}" if path else _norm_key(key))
        elif isinstance(obj, list):
            for pos, value in enumerate(obj):
                walk(value, depth + 1, f"{path}[{pos}]")

    walk(payload)
    if not found:
        return None
    # Campo direto tabela_N tem precedência sobre objetos genéricos.
    found.sort(key=lambda r: (0 if str(r.get("source", "")).startswith("payload:tabela") or str(r.get("source", "")).startswith("payload:table") else 1, str(r.get("source", ""))))
    return found[0]


def _price_for_table(prices: list[dict], table_ref: dict | None) -> dict | None:
    if not table_ref or not prices:
        return None
    idx = table_ref.get("index")
    label_norm = _norm_key(table_ref.get("label") or "")
    technical_norm = _norm_key(table_ref.get("technical") or "")
    raw_norm = _norm_key(table_ref.get("raw") or "")

    # Primeiro usa o índice técnico já extraído do payload, quando disponível.
    for pr in prices:
        if idx is not None and pr.get("table_index") is not None:
            try:
                if int(pr.get("table_index")) == int(idx):
                    return pr
            except Exception:
                pass
    # Canonicaliza cada preço usando tanto a descrição quanto o código técnico.
    for pr in prices:
        refs = []
        for value in (pr.get("table_code"), pr.get("table")):
            ref = _canonical_price_table_ref(value) if value not in (None, "") else None
            if ref:
                refs.append(ref)
        if idx is not None and any(ref.get("index") == idx for ref in refs):
            return pr
    for pr in prices:
        candidates = {_norm_key(pr.get("table") or ""), _norm_key(pr.get("table_code") or "")}
        if label_norm and label_norm in candidates:
            return pr
        if technical_norm and technical_norm in candidates:
            return pr
        if raw_norm and raw_norm in candidates:
            return pr
    return None



def _rule_text(value: Any) -> str:
    return _norm_key(str(value or "")).strip()


def _commercial_price_rule_context(db, cid: int, partner_ids: set[int]) -> tuple[dict[int, list[CommercialPriceRule]], dict[int, CollectionCustomerRule]]:
    by_partner: dict[int, list[CommercialPriceRule]] = defaultdict(list)
    contract_by_partner: dict[int, CollectionCustomerRule] = {}
    if not partner_ids:
        return by_partner, contract_by_partner
    rules = db.scalars(select(CommercialPriceRule).where(
        CommercialPriceRule.company_id == cid,
        CommercialPriceRule.partner_id.in_(list(partner_ids)),
        CommercialPriceRule.active == True,
    ).order_by(CommercialPriceRule.id.desc())).all()
    for rule in rules:
        by_partner[int(rule.partner_id)].append(rule)
    contracts = db.scalars(select(CollectionCustomerRule).where(
        CollectionCustomerRule.company_id == cid,
        CollectionCustomerRule.partner_id.in_(list(partner_ids)),
        CollectionCustomerRule.has_collection_contract == True,
    )).all()
    contract_by_partner = {int(r.partner_id): r for r in contracts}
    return by_partner, contract_by_partner


def _rule_in_force(rule: CommercialPriceRule, sale_date: str | None) -> bool:
    current = str(sale_date or "")[:10]
    if rule.valid_from and current and current < str(rule.valid_from)[:10]:
        return False
    if rule.valid_to and current and current > str(rule.valid_to)[:10]:
        return False
    return True


def _resolve_authorized_price_rule(
    item,
    product_info: dict,
    base_price: float | None,
    sale_date: str | None,
    partner_id: int | None,
    rules_by_partner: dict[int, list[CommercialPriceRule]],
    contract_by_partner: dict[int, CollectionCustomerRule],
) -> dict | None:
    if not partner_id:
        return None
    meta = _product_row_meta(product_info.get("product"), item.product_code, item.description, product_info.get("payload") or {})
    product = product_info.get("product")
    product_keys = {
        _rule_text(item.product_code), _rule_text(item.product_id), _rule_text(item.ean), _rule_text(item.description),
        _rule_text(getattr(product, "code", "")), _rule_text(getattr(product, "erpflex_id", "")), _rule_text(getattr(product, "ean", "")), _rule_text(getattr(product, "description", "")),
    }
    product_keys.discard("")
    category = _rule_text(meta.get("category"))
    subcategory = _rule_text(meta.get("subcategory"))
    candidates: list[tuple[int, int, CommercialPriceRule]] = []
    priority = {"PRODUCT": 400, "SUBCATEGORY": 300, "CATEGORY": 200, "GENERAL": 100}
    for rule in rules_by_partner.get(int(partner_id), []):
        if not _rule_in_force(rule, sale_date):
            continue
        scope = str(rule.scope_type or "GENERAL").upper()
        ref = _rule_text(rule.scope_value)
        matches = (
            scope == "GENERAL" or
            (scope == "PRODUCT" and ref and ref in product_keys) or
            (scope == "CATEGORY" and ref and ref == category) or
            (scope == "SUBCATEGORY" and ref and ref == subcategory)
        )
        if matches:
            candidates.append((priority.get(scope, 0), int(rule.id or 0), rule))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for prio, _rid, rule in candidates:
        kind = str(rule.rule_type or "DISCOUNT_PERCENT").upper()
        value = float(rule.rule_value or 0)
        expected = None
        if kind == "FIXED_PRICE":
            expected = max(value, 0.0)
        elif base_price is not None and base_price > 0:
            if kind == "SURCHARGE_PERCENT":
                expected = base_price * (1.0 + value / 100.0)
            else:
                expected = base_price * (1.0 - value / 100.0)
        if expected is None:
            continue
        scope_label = {"PRODUCT":"Produto", "SUBCATEGORY":"Subcategoria", "CATEGORY":"Categoria", "GENERAL":"Cliente"}.get(str(rule.scope_type or "GENERAL").upper(), "Cliente")
        if kind == "FIXED_PRICE":
            label = f"Preço fixo autorizado · {scope_label}"
        elif kind == "SURCHARGE_PERCENT":
            label = f"Acréscimo autorizado {value:.2f}% · {scope_label}"
        else:
            label = f"Desconto autorizado {value:.2f}% · {scope_label}"
        return {
            "source": "REGRA_COMERCIAL", "rule_id": int(rule.id), "label": label,
            "scope": str(rule.scope_type or "GENERAL").upper(), "scope_value": rule.scope_value or "",
            "rule_type": kind, "rule_value": round(value, 4), "expected": round(max(expected, 0.0), 4),
            "notes": rule.notes or "", "priority": prio,
        }
    contract = contract_by_partner.get(int(partner_id))
    if contract and str(contract.contract_discount_application or "FINANCEIRO").upper() == "PRECO" and base_price is not None and base_price > 0:
        pct = max(0.0, min(float(contract.contract_discount_percent or 0), 100.0))
        if pct > 0:
            return {
                "source": "CONTRATO", "rule_id": int(contract.id), "label": f"Contrato · desconto autorizado {pct:.2f}%",
                "scope": "CONTRACT", "scope_value": "", "rule_type": "DISCOUNT_PERCENT", "rule_value": round(pct, 4),
                "expected": round(base_price * (1.0 - pct / 100.0), 4), "notes": contract.notes or "", "priority": 50,
            }
    return None


def _sales_price_audit(db, cid: int, start: date, end: date) -> dict:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return {"title": "Faturamento · auditoria de preço", "kind": "price_audit", "summary": {"invoices": 0, "items": 0, "audited": 0, "divergent": 0, "below": 0, "above": 0, "missing": 0, "authorized": 0, "authorized_ok": 0, "impact": 0.0}, "rows": [], "note": "Sem NF-e válidas no período."}
    invoice_ids = [int(r.id) for r in sales]
    items = db.execute(select(
        SalesInvoiceItem.id, SalesInvoiceItem.sales_invoice_id, SalesInvoiceItem.product_id,
        SalesInvoiceItem.product_code, SalesInvoiceItem.ean, SalesInvoiceItem.description,
        SalesInvoiceItem.qty, SalesInvoiceItem.unit_price, SalesInvoiceItem.total,
    ).where(SalesInvoiceItem.sales_invoice_id.in_(invoice_ids))).all()
    if not items:
        return {"title": "Faturamento · auditoria de preço", "kind": "price_audit", "summary": {"invoices": len(sales), "items": 0, "audited": 0, "divergent": 0, "below": 0, "above": 0, "missing": 0, "authorized": 0, "authorized_ok": 0, "impact": 0.0}, "rows": [], "note": "As NF-e do período não possuem itens sincronizados para auditoria."}
    invoice_by_id = {int(r.id): r for r in sales}
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    seller_for = _seller_resolver_context(db, cid, sales)
    invoice_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in sales if r.raw_record_id])
    product_resolver = _product_price_reference_context(db, cid, items)
    rules_by_partner, contract_by_partner = _commercial_price_rule_context(db, cid, {int(r.customer_id) for r in sales if r.customer_id})

    # tabela da venda por NF. A mesma tabela é aplicada aos itens daquela venda.
    # Quando a NF não trouxer a referência diretamente, tenta o pedido vinculado.
    order_rows = db.execute(select(SalesOrder.erpflex_id, SalesOrder.number, SalesOrder.raw_record_id).where(SalesOrder.company_id == cid)).all()
    order_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in order_rows if r.raw_record_id])
    order_by_id: dict[str, dict] = {}
    order_by_number: dict[str, dict] = {}
    for order in order_rows:
        payload = order_payloads.get(int(order.raw_record_id), {}) if order.raw_record_id else {}
        if not payload:
            continue
        oid = str(order.erpflex_id or "").strip()
        onum = str(order.number or "").strip()
        if oid:
            order_by_id[oid] = payload
        if onum:
            order_by_number[onum] = payload
    table_by_invoice: dict[int, dict | None] = {}
    for inv in sales:
        payload = invoice_payloads.get(int(inv.raw_record_id), {}) if inv.raw_record_id else {}
        ref = _extract_sale_price_table(payload)
        if not ref:
            oid = str(_walk_find(payload, {"orcamento_id", "id_orcamento", "pedido_id", "id_pedido", "solicitacao_id", "id_solicitacao"}) or "").strip()
            onum = str(_walk_find(payload, {"numero_pedido", "pedido_numero", "numero_orcamento"}) or "").strip()
            order_payload = order_by_id.get(oid) if oid else None
            if not order_payload and onum:
                order_payload = order_by_number.get(onum)
            if order_payload:
                ref = _extract_sale_price_table(order_payload)
                if ref:
                    ref = {**ref, "source": f"pedido:{ref.get('source') or 'tabela_preco'}"}
        table_by_invoice[int(inv.id)] = ref

    summary = {
        "invoices": len(sales), "items": len(items), "audited": 0, "divergent": 0, "below": 0, "above": 0,
        "missing": 0, "authorized": 0, "authorized_ok": 0, "impact": 0.0,
        "invoice_table_identified": sum(1 for ref in table_by_invoice.values() if ref),
        "invoice_table_missing": sum(1 for ref in table_by_invoice.values() if not ref),
        "product_matched": 0, "product_with_prices": 0, "table_price_matched": 0, "table_price_unmatched": 0,
        "technical_key_matched": 0, "technical_key_missing_table_id": 0, "technical_key_missing_product_code": 0,
        "missing_no_table": 0, "missing_no_product": 0, "missing_no_prices": 0, "missing_table_mismatch": 0,
    }
    grouped: dict[int, dict] = {}
    for item in items:
        inv = invoice_by_id.get(int(item.sales_invoice_id))
        if not inv:
            continue
        table_ref = table_by_invoice.get(int(inv.id))
        product_info = product_resolver(item)
        if product_info.get("matched"):
            summary["product_matched"] += 1
        if product_info.get("prices"):
            summary["product_with_prices"] += 1
        # V1.3.79: vínculo técnico principal = ID da tabela da venda + código do produto faturado.
        # O parser por nome fica apenas como fallback.
        table_idx = table_ref.get("index") if table_ref else None
        product_code = str(item.product_code or "").strip()
        direct_reference = None
        if table_idx is None:
            summary["technical_key_missing_table_id"] += 1
        if not product_code:
            summary["technical_key_missing_product_code"] += 1
        if table_idx is not None and product_code and product_info.get("match_method") == "product_code":
            direct_reference = _direct_price_from_product_payload(product_info.get("payload"), table_ref)
            if direct_reference:
                summary["technical_key_matched"] += 1
        reference = direct_reference or _price_for_table(product_info["prices"], table_ref)
        if table_ref and product_info.get("prices"):
            if reference:
                summary["table_price_matched"] += 1
            else:
                summary["table_price_unmatched"] += 1
        actual = round(float(item.unit_price or 0), 4)
        qty = float(item.qty or 0)
        base_expected = round(float(reference.get("price") or 0), 4) if reference and float(reference.get("price") or 0) > 0 else None
        special_rule = _resolve_authorized_price_rule(
            item, product_info, base_expected, inv.issue_date, int(inv.customer_id) if inv.customer_id else None,
            rules_by_partner, contract_by_partner,
        )
        expected = round(float(special_rule.get("expected") or 0), 4) if special_rule else base_expected
        if expected is None or expected <= 0:
            summary["missing"] += 1
            if not table_ref:
                summary["missing_no_table"] += 1
            elif not product_info.get("matched"):
                summary["missing_no_product"] += 1
            elif not product_info.get("prices"):
                summary["missing_no_prices"] += 1
            elif not reference:
                summary["missing_table_mismatch"] += 1
            continue
        if special_rule:
            summary["authorized"] += 1
        summary["audited"] += 1
        diff_unit = round(actual - expected, 4)
        # preço monetário é comparado por centavo para evitar falso positivo de arredondamento
        if abs(diff_unit) < 0.005:
            if special_rule:
                summary["authorized_ok"] += 1
            continue
        diff_pct = round((diff_unit / expected) * 100.0, 2) if expected else 0.0
        impact = round(diff_unit * qty, 2)
        summary["divergent"] += 1
        summary["below" if diff_unit < 0 else "above"] += 1
        summary["impact"] += impact
        partner = partners.get(int(inv.customer_id or 0))
        client = (partner.trade_name or partner.name) if partner else "Sem cliente identificado"
        row = {
            "item_id": int(item.id), "invoice_id": int(inv.id), "invoice": inv.number or "—",
            "date": inv.issue_date or "—", "client": client, "seller": seller_for(inv),
            "table": (table_ref.get("label") or table_ref.get("raw") or "Não identificada") if table_ref else "Sem tabela identificada",
            "table_code": (table_ref.get("technical") or (f"Tabela {table_ref.get('index')}" if table_ref.get("index") is not None else "")) if table_ref else "",
            "product_code": item.product_code or "—", "ean": item.ean or "—",
            "product": item.description or "Sem descrição", "qty": round(qty, 3),
            "table_expected": base_expected, "expected": expected, "actual": actual, "diff_unit": diff_unit,
            "reference_match": (reference.get("match") if reference else "") or ("fallback" if reference else ""),
            "reference_source": reference.get("source") if reference else "",
            "diff_pct": diff_pct, "impact": impact, "direction": "below" if diff_unit < 0 else "above",
            "authorized_rule": special_rule.get("label") if special_rule else "",
            "authorized_source": special_rule.get("source") if special_rule else "",
            "authorized_notes": special_rule.get("notes") if special_rule else "",
            "line_total": _money(item.total),
        }
        cell = grouped.setdefault(int(inv.id), {
            "invoice_id": int(inv.id), "invoice": inv.number or "—", "date": inv.issue_date or "—",
            "client": client, "seller": seller_for(inv),
            "table": (table_ref.get("label") or table_ref.get("raw") or "Não identificada") if table_ref else "Sem tabela identificada",
            "table_code": (table_ref.get("technical") or "") if table_ref else "", "count": 0, "below": 0, "above": 0,
            "authorized": 0, "impact": 0.0, "items": [],
        })
        cell["count"] += 1
        if special_rule:
            cell["authorized"] += 1
        cell[row["direction"]] += 1
        cell["impact"] += impact
        cell["items"].append(row)
    summary["impact"] = round(summary["impact"], 2)
    rows = []
    for cell in grouped.values():
        cell["impact"] = round(cell["impact"], 2)
        cell["items"].sort(key=lambda r: abs(r["diff_pct"]), reverse=True)
        rows.append(cell)
    rows.sort(key=lambda r: abs(r["impact"]), reverse=True)
    return {
        "title": "Faturamento · auditoria de preço", "kind": "price_audit",
        "summary": summary, "rows": rows,
        "note": "Compara o preço praticado com o preço esperado após aplicar a política comercial autorizada. O vínculo principal de referência usa o ID da tabela praticada na venda + o código do produto faturado para localizar o preço da mesma tabela no cadastro do produto. ID ERP, EAN e nomes ficam apenas como fallback. Depois aplica a política autorizada: produto → subcategoria → categoria → regra geral do cliente → desconto de contrato aplicado ao preço. Diferenças inferiores a meio centavo são tratadas como arredondamento.",
    }


def _product_price_lookup(db, cid: int, query: str = "") -> dict:
    q = str(query or "").strip()
    if not q:
        return {"title": "Comercial · preço por tabela", "kind": "price_lookup", "query": "", "rows": [], "note": "Digite código, descrição ou EAN do produto para consultar preços sincronizados da API."}
    like = f"%{q}%"
    products = db.scalars(select(Product).where(
        Product.company_id == cid,
        or_(Product.code.ilike(like), Product.description.ilike(like), Product.ean.ilike(like), Product.erpflex_id.ilike(like)),
    ).order_by(Product.description.asc()).limit(30)).all()
    ext_ids = [str(p.erpflex_id) for p in products if p.erpflex_id]
    raw_map: dict[str, dict] = {}
    if ext_ids:
        raws = db.execute(select(RawRecord.external_id, RawRecord.payload_json).where(
            RawRecord.company_id == cid,
            func.upper(RawRecord.source) == "ERPFLEX",
            func.lower(RawRecord.module) == "products",
            RawRecord.external_id.in_(ext_ids),
        ).order_by(RawRecord.id.desc())).all()
        for external_id, payload_json in raws:
            key = str(external_id or "")
            if key in raw_map:
                continue
            try:
                payload = json.loads(payload_json or "{}")
                if isinstance(payload, dict):
                    raw_map[key] = payload
            except Exception:
                pass
    rows = []
    for p in products:
        payload = raw_map.get(str(p.erpflex_id or ""), {})
        prices = _extract_price_tables(payload)
        rows.append({
            "id": int(p.id), "code": p.code or "—", "ean": p.ean or "—", "name": p.description or "Sem descrição",
            "category": p.category or "Sem categoria", "subcategory": p.subcategory or "Sem subcategoria",
            "prices": prices,
        })
    return {"title": "Comercial · preço por tabela", "kind": "price_lookup", "query": q, "rows": rows, "note": "Somente preços efetivamente presentes no retorno sincronizado de produtos são exibidos."}


def _sales_grouped_by_terms(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    for inv in sales:
        partner = partners.get(int(inv.customer_id or 0))
        name = str((partner.payment_terms if partner else None) or "Sem prazo cadastrado").strip()
        invoice = {"id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—", "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro", "value": _money(inv.total)}
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _sales_grouped_by_date(db, cid: int, start: date, end: date) -> list[dict]:
    rows = _sales_invoice_rows(db, cid, start, end)
    acc: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("date") or "Sem data")
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(row.get("value"))
        cell["invoices"].append(row)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["name"], reverse=True)


def _agenda_grouped(db, cid: int, days: int = 7, limit: int = 100) -> dict:
    rows = _agenda(db, cid, days, limit)
    acc: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("due") or "Sem vencimento")
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "rows": []})
        cell["count"] += 1
        cell["value"] += _money(row.get("value"))
        cell["rows"].append(row)
    groups = sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["name"])
    return {"title": "Agenda financeira · próximos 7 dias", "kind": "agenda_grouped", "rows": groups, "total": round(sum(_money(g["value"]) for g in groups), 2)}


def _cash_grouped(db, cid: int, start: date, end: date) -> dict:
    rec = _grouped_financial_detail("Recebimentos", _financial_group_rows(db, cid, "RECEBER", "paid", start, end), "", "paid", "Cliente")
    pay = _grouped_financial_detail("Pagamentos", _financial_group_rows(db, cid, ["PAGAR", "DESPESA"], "paid", start, end), "", "paid", "Fornecedor")
    return {"title": "Movimentação financeira do período", "kind": "cash_grouped", "receivables": rec, "payables": pay}


def _seller_resolver_context(db, cid: int, sales: list):
    invoice_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in sales if r.raw_record_id])
    order_rows = db.execute(select(
        SalesOrder.erpflex_id, SalesOrder.number, SalesOrder.seller_id,
        SalesOrder.seller_name, SalesOrder.raw_record_id
    ).where(SalesOrder.company_id == cid)).all()
    order_payloads = _load_raw_payloads(db, [int(r.raw_record_id) for r in order_rows if r.raw_record_id])
    by_id: dict[str, str] = {}
    by_number: dict[str, list[str]] = defaultdict(list)
    for order in order_rows:
        info = _seller_info(order_payloads.get(int(order.raw_record_id), {}) if order.raw_record_id else {})
        seller = str(order.seller_name or info.get("name") or (f"Vendedor ID {order.seller_id or info.get('id')}" if (order.seller_id or info.get("id")) else "")).strip()
        if not seller:
            continue
        oid = str(order.erpflex_id or "").strip()
        onum = str(order.number or "").strip()
        if oid:
            by_id[oid] = seller
        if onum and seller not in by_number[onum]:
            by_number[onum].append(seller)

    def seller_for(inv) -> str:
        payload = invoice_payloads.get(int(inv.raw_record_id), {}) if inv.raw_record_id else {}
        direct = _seller_info(payload).get("display")
        if direct:
            return str(direct)
        oid = str(_walk_find(payload, {"orcamento_id", "id_orcamento", "pedido_id", "id_pedido", "solicitacao_id", "id_solicitacao"}) or "").strip()
        if oid and oid in by_id:
            return by_id[oid]
        onum = str(_walk_find(payload, {"numero_pedido", "pedido_numero", "numero_orcamento", "documento"}) or "").strip()
        candidates = by_number.get(onum, []) if onum else []
        return candidates[0] if len(candidates) == 1 else "Sem vendedor identificado"
    return seller_for


def _commercial_grouped_by_seller(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    if not sales:
        return []
    seller_for = _seller_resolver_context(db, cid, sales)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    for inv in sales:
        name = seller_for(inv)
        partner = partners.get(int(inv.customer_id or 0))
        invoice = {"id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—", "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro", "value": _money(inv.total)}
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)

def _product_meta_context(db, cid: int):
    products = db.scalars(select(Product).where(Product.company_id == cid)).all()
    by_code = {str(p.code or "").strip(): p for p in products if str(p.code or "").strip()}
    by_external = {str(p.erpflex_id or "").strip(): p for p in products if str(p.erpflex_id or "").strip()}
    raw_rows = db.execute(select(RawRecord.external_id, RawRecord.payload_json).where(
        RawRecord.company_id == cid,
        func.upper(RawRecord.source) == "ERPFLEX",
        func.lower(RawRecord.module) == "products",
    )).all()
    raw_by_external: dict[str, dict] = {}
    raw_by_code: dict[str, dict] = {}
    for external_id, payload_json in raw_rows:
        try:
            payload = json.loads(payload_json or "{}")
            if not isinstance(payload, dict):
                continue
        except Exception:
            continue
        ext = str(external_id or "").strip()
        if ext:
            raw_by_external[ext] = payload
        raw_code = _walk_find(payload, {"codigo", "codigo_produto", "sku", "referencia"})
        if raw_code not in (None, ""):
            raw_by_code[str(raw_code).strip()] = payload
    def resolve(code: Any, description: Any, product_id: Any = None) -> dict:
        code_text = str(code or "").strip()
        pid_text = str(product_id or "").strip()
        product = by_code.get(code_text) or by_external.get(pid_text)
        payload = raw_by_code.get(code_text) or raw_by_external.get(pid_text)
        if not payload and product:
            payload = raw_by_external.get(str(product.erpflex_id or "").strip())
        return _product_row_meta(product, code_text, description, payload)
    return resolve


def _financial_raw_partner_identity(payload: dict, kind: str, fallback: dict) -> dict:
    """Resolve a identidade do parceiro diretamente do título bruto.

    O partner_id legado pode estar consolidado por nome em bases antigas. Para o BI,
    CNPJ/CPF e ID específico do cliente/fornecedor do próprio lançamento têm prioridade.
    """
    role_customer = str(kind or "").upper() == "RECEBER"
    if role_customer:
        tax_keys = {"cliente_cnpj", "cnpj_cliente", "cpf_cnpj_cliente", "cpf_cnpj", "cnpj_cpf", "cnpjcpf", "documento_cliente"}
        id_keys = {"cliente_id", "id_cliente", "codigo_cliente", "cliente_codigo", "idcliente"}
        name_keys = {"cliente_desc", "cliente_nome", "nome_cliente", "razao_social_cliente", "fantasia_cliente"}
        nested_keys = {"cliente", "customer"}
    else:
        tax_keys = {"fornecedor_cnpj", "cnpj_fornecedor", "cpf_cnpj_fornecedor", "cnpj_cpf", "cnpjcpf", "documento_fornecedor"}
        id_keys = {"fornecedor_id", "id_fornecedor", "codigo_fornecedor", "fornecedor_codigo", "idfornecedor"}
        name_keys = {"fornecedor_desc", "fornecedor_nome", "nome_fornecedor", "razao_social_fornecedor", "fantasia_fornecedor"}
        nested_keys = {"fornecedor", "supplier"}

    tax_raw = _walk_find(payload or {}, tax_keys)
    erp_raw = _walk_find(payload or {}, id_keys)
    name_raw = _walk_find(payload or {}, name_keys)
    nested = _walk_find(payload or {}, nested_keys)
    if isinstance(nested, dict):
        low = {str(k).lower(): v for k, v in nested.items()}
        if tax_raw in (None, ""):
            for key in ("cnpj", "cpf", "cnpj_cpf", "cnpjcpf", "cpf_cnpj", "documento"):
                if low.get(key) not in (None, ""):
                    tax_raw = low.get(key); break
        if erp_raw in (None, ""):
            for key in ("id", "codigo", "erpflex_id", "cliente_id", "fornecedor_id"):
                if low.get(key) not in (None, ""):
                    erp_raw = low.get(key); break
        if name_raw in (None, ""):
            for key in ("nome_fantasia", "fantasia", "razao_social", "nome", "descricao", "desc"):
                if low.get(key) not in (None, ""):
                    name_raw = low.get(key); break

    tax = re.sub(r"\D", "", str(tax_raw or ""))
    erp = str(erp_raw or "").strip()
    name = str(name_raw or fallback.get("partner") or "Sem cadastro").strip() or "Sem cadastro"
    if tax:
        return {"key": f"tax:{tax}", "name": name, "subtitle": f"CNPJ/CPF {_format_cnpj_cpf(tax)}", "cnpj": tax, "erp": erp}
    if erp:
        return {"key": f"rawerp:{erp}", "name": name, "subtitle": f"Cliente/Fornecedor ERP {erp}", "cnpj": "", "erp": erp}

    fallback_cnpj = re.sub(r"\D", "", str(fallback.get("partner_cnpj") or ""))
    if fallback_cnpj:
        return {"key": f"tax:{fallback_cnpj}", "name": name, "subtitle": f"CNPJ/CPF {_format_cnpj_cpf(fallback_cnpj)}", "cnpj": fallback_cnpj, "erp": str(fallback.get("partner_erpflex_id") or "")}
    fallback_erp = str(fallback.get("partner_erpflex_id") or "").strip()
    if fallback_erp:
        return {"key": f"erp:{fallback_erp}", "name": name, "subtitle": f"Cadastro ERP {fallback_erp}", "cnpj": "", "erp": fallback_erp}
    pid = fallback.get("partner_id")
    if pid not in (None, ""):
        return {"key": f"id:{pid}", "name": name, "subtitle": f"Cadastro {pid}", "cnpj": "", "erp": ""}
    return {"key": f"name:{_norm_key(name)}", "name": name, "subtitle": "Sem identificador fiscal/ERP no título", "cnpj": "", "erp": ""}


def _fast_finance_catalogs(db, cid: int) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    raws = db.scalars(select(RawRecord).where(
        RawRecord.company_id == cid, RawRecord.source == "ERPFLEX",
        RawRecord.module.in_(["banks", "bank_detail", "boleto_options"])
    )).all()
    raw_map = {int(r.id): r for r in raws}
    banks = dict(VERIFIED_BANK_NAMES)
    banks.update(_bank_catalog(raws, raw_map))
    wallets = _wallet_catalog(raws, raw_map)
    return banks, wallets


def _resolved_financial_bank(stored: str, payload: dict, detail: dict, catalog: dict[str, str]) -> tuple[str, str]:
    dref = financial_bank_wallet_fields(detail or {})
    pref = financial_bank_wallet_fields(payload or {})
    bank_id = str(dref.get("bank_id") or pref.get("bank_id") or "").strip()
    bank_desc = str(dref.get("bank_desc") or pref.get("bank_desc") or "").strip()
    stored_text = str(stored or "").strip()
    low = stored_text.lower()
    if not bank_id and low.startswith("banco "):
        possible = stored_text.split(" ", 1)[1].strip()
        if possible.isdigit():
            bank_id = possible
    elif not bank_id and stored_text.isdigit():
        bank_id = stored_text
    stored_specific = ""
    if stored_text and not (low.startswith("banco ") or low.startswith("sem banco") or low.startswith("banco id ") or stored_text.isdigit()):
        stored_specific = stored_text
    label = bank_desc or catalog.get(bank_id, "") or stored_specific
    if not label:
        label = "" if bank_id in {"", "0"} else f"Banco {bank_id}"
    return label, bank_id


def _resolved_financial_wallet(stored: str, payload: dict, detail: dict, catalog: dict[tuple[str, str], str], bank_id: str = "") -> tuple[str, str]:
    dref = financial_bank_wallet_fields(detail or {})
    pref = financial_bank_wallet_fields(payload or {})
    wallet_id = str(dref.get("wallet_id") or pref.get("wallet_id") or "").strip()
    wallet_desc = str(dref.get("wallet_desc") or pref.get("wallet_desc") or "").strip()
    stored_text = str(stored or "").strip()
    low = stored_text.lower()
    if not wallet_id and low.startswith("carteira "):
        possible = stored_text.split(" ", 1)[1].strip()
        if possible.isdigit():
            wallet_id = possible
    elif not wallet_id and stored_text.isdigit():
        wallet_id = stored_text
    stored_specific = ""
    if stored_text and not (low.startswith("carteira ") or low.startswith("sem carteira") or low.startswith("carteira não") or stored_text.isdigit()):
        stored_specific = stored_text
    label = wallet_desc or catalog.get((bank_id, wallet_id), "") or catalog.get(("", wallet_id), "") or stored_specific
    if not label:
        label = "" if wallet_id in {"", "0"} else f"Carteira {wallet_id}"
    return label, wallet_id


def _collection_group_label(bank: str, bank_id: str, wallet: str, wallet_id: str) -> tuple[str, str]:
    b = str(bank or "").strip()
    w = str(wallet or "").strip()
    if b:
        return b, "Cobrança bancária"
    if w:
        return f"Carteira · {w}", "Cobrança por carteira, sem banco vinculado"
    return "Cobrança não identificada", "Banco e carteira ausentes na integração"


def _financial_group_rows(db, cid: int, kind: str | list[str], status: str = "open", start: date | None = None, end: date | None = None) -> list[dict]:
    # Carrega todos os títulos da visão e resolve a identidade pelo payload bruto.
    # Isso corrige bases antigas onde parceiros com a mesma razão/fantasia foram
    # associados ao mesmo Partner antes de o CNPJ/ID específico estar disponível.
    bal = _balance_expr()
    label = _partner_label_expr()
    kind_condition = FinancialTitle.kind.in_(kind) if isinstance(kind, list) else (FinancialTitle.kind == kind)
    conditions = [FinancialTitle.company_id == cid, kind_condition]
    today = date.today().isoformat()
    if status == "open":
        conditions.append(bal > 0.005)
    elif status == "overdue":
        conditions.extend([bal > 0.005, FinancialTitle.due_date < today])
    elif status == "due":
        conditions.extend([bal > 0.005, FinancialTitle.due_date >= today])
    elif status == "paid" and start and end:
        conditions.extend([FinancialTitle.paid_date >= start.isoformat(), FinancialTitle.paid_date <= end.isoformat(), FinancialTitle.paid_value > 0.005])
    rows = db.execute(select(
        FinancialTitle.id, FinancialTitle.erpflex_id, FinancialTitle.document, FinancialTitle.issue_date, FinancialTitle.due_date,
        FinancialTitle.paid_date, FinancialTitle.partner_id, FinancialTitle.raw_record_id, FinancialTitle.kind, label.label("partner"),
        Partner.cnpj_cpf.label("partner_cnpj"), Partner.erpflex_id.label("partner_erpflex_id"),
        FinancialTitle.value, FinancialTitle.paid_value, FinancialTitle.bank, FinancialTitle.wallet, bal.label("balance")
    ).outerjoin(Partner, Partner.id == FinancialTitle.partner_id).where(*conditions)).all()
    raw_map = _load_raw_payloads(db, [int(r.raw_record_id) for r in rows if r.raw_record_id])
    ext_ids = [str(r.erpflex_id or "").strip() for r in rows if str(r.erpflex_id or "").strip()]
    detail_map: dict[str, dict] = {}
    if ext_ids and (kind == "RECEBER" or (isinstance(kind, list) and "RECEBER" in kind)):
        details = db.scalars(select(RawRecord).where(
            RawRecord.company_id == cid, RawRecord.source == "ERPFLEX",
            RawRecord.module == "receber_detalhe", RawRecord.external_id.in_(ext_ids)
        ).order_by(RawRecord.id)).all()
        for raw in details:
            try:
                payload = json.loads(raw.payload_json or "{}")
                if isinstance(payload, dict):
                    detail_map[str(raw.external_id or "")] = payload
            except Exception:
                pass
    bank_catalog, wallet_catalog = _fast_finance_catalogs(db, cid)
    out = []
    for r in rows:
        base = {
            "id": int(r.id), "document": r.document or "—", "partner": str(r.partner or "Sem cadastro"),
            "partner_id": int(r.partner_id) if r.partner_id else None,
            "partner_cnpj": str(r.partner_cnpj or "").strip(), "partner_erpflex_id": str(r.partner_erpflex_id or "").strip(),
            "issue": r.issue_date or "—", "due": r.due_date or "—", "paid_date": r.paid_date or "—",
            "value": _money(r.value), "paid": _money(r.paid_value), "balance": _money(r.balance),
            "bank": str(r.bank or "").strip(), "wallet": str(r.wallet or "").strip(),
        }
        payload = raw_map.get(int(r.raw_record_id), {}) if r.raw_record_id else {}
        detail = detail_map.get(str(r.erpflex_id or ""), {})
        resolved_bank, resolved_bank_id = _resolved_financial_bank(str(r.bank or ""), payload, detail, bank_catalog)
        resolved_wallet, resolved_wallet_id = _resolved_financial_wallet(str(r.wallet or ""), payload, detail, wallet_catalog, resolved_bank_id)
        base["bank"] = resolved_bank
        base["bank_id"] = resolved_bank_id
        base["wallet"] = resolved_wallet
        base["wallet_id"] = resolved_wallet_id
        collection_group, collection_note = _collection_group_label(resolved_bank, resolved_bank_id, resolved_wallet, resolved_wallet_id)
        base["collection_group"] = collection_group
        base["collection_note"] = collection_note
        ident = _financial_raw_partner_identity(payload, str(r.kind or kind), base)
        base["partner"] = ident["name"]
        base["identity_key"] = ident["key"]
        base["identity_subtitle"] = ident["subtitle"]
        if ident.get("cnpj"):
            base["partner_cnpj"] = ident["cnpj"]
        if ident.get("erp"):
            base["partner_erpflex_id"] = ident["erp"]
        out.append(base)
    return out

def _group_rows_by_field(rows: list[dict], field: str, value_field: str = "balance") -> list[dict]:
    acc: dict[str, dict] = {}
    for row in rows:
        name = str(row.get(field) or "Sem identificação")
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "titles": []})
        cell["count"] += 1
        cell["value"] += _money(row.get(value_field))
        cell["titles"].append(row)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


def _grouped_titles_detail(title: str, rows: list[dict], note: str = "", partner_label: str = "Grupo") -> dict:
    total = round(sum(_money(r.get("value")) for r in rows), 2)
    rows = sorted(rows, key=lambda r: _money(r.get("value")), reverse=True)[:60]
    for row in rows:
        row["share"] = _pct(_money(row.get("value")), total) if total else 0.0
        row["titles"] = sorted(row.get("titles") or [], key=lambda r: str(r.get("due") or ""), reverse=True)[:60]
    return {"title": title, "kind": "grouped_financial", "rows": rows, "total": total, "note": note, "partner_label": partner_label}


def _aging_grouped_rows(rows: list[dict]) -> list[dict]:
    names = ["A vencer", "Vence hoje", "1–30 dias vencidos", "31–60 dias vencidos", "61–90 dias vencidos", "> 90 dias vencidos", "Sem vencimento"]
    acc = {name: {"name": name, "count": 0, "value": 0.0, "titles": []} for name in names}
    today = date.today()
    for row in rows:
        due_raw = row.get("due")
        if not due_raw or due_raw == "—":
            bucket = "Sem vencimento"
        else:
            try:
                due = datetime.fromisoformat(str(due_raw)[:10]).date()
                delta = (today - due).days
                if delta < 0: bucket = "A vencer"
                elif delta == 0: bucket = "Vence hoje"
                elif delta <= 30: bucket = "1–30 dias vencidos"
                elif delta <= 60: bucket = "31–60 dias vencidos"
                elif delta <= 90: bucket = "61–90 dias vencidos"
                else: bucket = "> 90 dias vencidos"
            except Exception:
                bucket = "Sem vencimento"
        acc[bucket]["count"] += 1
        acc[bucket]["value"] += _money(row.get("balance"))
        acc[bucket]["titles"].append(row)
    return [{**acc[name], "value": round(acc[name]["value"], 2)} for name in names if acc[name]["count"]]

def _commercial_grouped_by_product(db, cid: int, start: date, end: date, mode: str) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    invoice_ids = [int(r.id) for r in sales]
    if not invoice_ids:
        return []
    items = db.execute(select(
        SalesInvoiceItem.product_id, SalesInvoiceItem.product_code, SalesInvoiceItem.description,
        SalesInvoiceItem.total, SalesInvoiceItem.sales_invoice_id,
    ).where(SalesInvoiceItem.sales_invoice_id.in_(invoice_ids))).all()
    resolve_meta = _product_meta_context(db, cid)
    seller_for = _seller_resolver_context(db, cid, sales)
    sales_map = {int(r.id): r for r in sales}
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    acc: dict[str, dict] = {}
    seen_invoice: dict[tuple[str, int], dict] = {}
    seen_seller_invoice: dict[tuple[str, str, int], dict] = {}

    for item in items:
        meta = resolve_meta(item.product_code, item.description, getattr(item, "product_id", None))
        category = meta["category"]
        subcategory = meta["subcategory"]
        name = category if mode == "category" else f"{category} › {subcategory}"
        inv = sales_map.get(int(item.sales_invoice_id or 0))
        if not inv:
            continue
        partner = partners.get(int(inv.customer_id or 0))
        seller = seller_for(inv)
        product_label = f"{meta['code']} · {meta['name']}"
        item_value = _money(item.total)

        cell = acc.setdefault(name, {
            "name": name, "count": 0, "item_count": 0, "value": 0.0,
            "products": {}, "invoices": [], "invoice_ids": set(), "sellers": {}
        })
        cell["item_count"] += 1
        cell["value"] += item_value
        cell["invoice_ids"].add(int(inv.id))

        prod = cell["products"].setdefault(product_label, {"name": product_label, "count": 0, "value": 0.0})
        prod["count"] += 1
        prod["value"] += item_value

        ikey = (name, int(inv.id))
        if ikey not in seen_invoice:
            inv_row = {
                "id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—",
                "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro",
                "value": 0.0, "items": [], "seller": seller,
            }
            seen_invoice[ikey] = inv_row
            cell["invoices"].append(inv_row)
        inv_row = seen_invoice[ikey]
        inv_row["value"] += item_value
        inv_row["items"].append({"item": product_label, "value": item_value})

        seller_cell = cell["sellers"].setdefault(seller, {
            "name": seller, "count": 0, "item_count": 0, "value": 0.0,
            "invoice_ids": set(), "invoices": []
        })
        seller_cell["item_count"] += 1
        seller_cell["value"] += item_value
        seller_cell["invoice_ids"].add(int(inv.id))
        skey = (name, seller, int(inv.id))
        if skey not in seen_seller_invoice:
            sinv = {
                "id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—",
                "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro",
                "value": 0.0,
            }
            seen_seller_invoice[skey] = sinv
            seller_cell["invoices"].append(sinv)
        seen_seller_invoice[skey]["value"] += item_value

    result = []
    for cell in acc.values():
        cell["value"] = round(cell["value"], 2)
        cell["count"] = len(cell.pop("invoice_ids"))
        cell["products"] = sorted(
            ({**p, "value": round(p["value"], 2)} for p in cell["products"].values()),
            key=lambda r: r["value"], reverse=True
        )
        for inv in cell["invoices"]:
            inv["value"] = round(inv["value"], 2)
        cell["invoices"].sort(key=lambda r: (r["date"], r["number"]), reverse=True)

        seller_rows = []
        for seller_cell in cell["sellers"].values():
            seller_cell["value"] = round(seller_cell["value"], 2)
            seller_cell["count"] = len(seller_cell.pop("invoice_ids"))
            for inv in seller_cell["invoices"]:
                inv["value"] = round(inv["value"], 2)
            seller_cell["invoices"].sort(key=lambda r: (r["date"], r["number"]), reverse=True)
            seller_cell["share"] = _pct(seller_cell["value"], cell["value"]) if cell["value"] else 0.0
            seller_rows.append(seller_cell)
        cell["sellers"] = sorted(seller_rows, key=lambda r: r["value"], reverse=True)
        result.append(cell)
    return sorted(result, key=lambda r: r["value"], reverse=True)

def _commercial_snapshot(db, cid: int, start: date, end: date) -> dict:
    sellers_all = _commercial_grouped_by_seller(db, cid, start, end)
    categories_all = _commercial_grouped_by_product(db, cid, start, end, "category")
    subcategories_all = _commercial_grouped_by_product(db, cid, start, end, "subcategory")
    def top_with_share(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        total = sum(_money(r.get("value")) for r in rows)
        row = dict(rows[0])
        row["share"] = _pct(_money(row.get("value")), total) if total else 0.0
        return row
    return {
        "seller": top_with_share(sellers_all),
        "category": top_with_share(categories_all),
        "subcategory": top_with_share(subcategories_all),
        "item_total": round(sum(_money(r.get("value")) for r in categories_all), 2),
    }

def build_fast_detail(db, cid: int, key: str, period: str = "month", date_from: str = "", date_to: str = "", query: str = "") -> dict:
    start, end = _period_bounds(period, date_from, date_to)
    key = ROOT_DETAIL.get(key, key)

    if key == "ar-open":
        detail = _grouped_financial_detail("Contas a receber em aberto", _financial_group_rows(db, cid, "RECEBER", "open"), "Agrupado por CNPJ/CPF/ID do cliente, não apenas pelo nome. Toque no cliente para ver os títulos.", "balance", "Cliente")
    elif key == "ar-overdue":
        detail = _grouped_financial_detail("Contas a receber · vencidos", _financial_group_rows(db, cid, "RECEBER", "overdue"), "Agrupado por CNPJ/CPF/ID do cliente, não apenas pelo nome. Toque no cliente para ver os títulos vencidos.", "balance", "Cliente")
    elif key == "ar-due":
        detail = _grouped_financial_detail("Contas a receber · a vencer", _financial_group_rows(db, cid, "RECEBER", "due"), "Agrupado por CNPJ/CPF/ID do cliente, não apenas pelo nome. Toque no cliente para ver os títulos a vencer.", "balance", "Cliente")
    elif key == "ar-received":
        detail = _grouped_financial_detail("Contas a receber · recebidos", _financial_group_rows(db, cid, "RECEBER", "paid", start, end), "Agrupado por CNPJ/CPF/ID do cliente, não apenas pelo nome. Toque no cliente para ver os recebimentos.", "paid", "Cliente")
    elif key == "ar-contract":
        detail = _grouped_contract_detail("Contas a receber · contratos", _contract_title_rows(db, cid, None), "Clientes marcados como cobrança por contrato. Toque no cliente para ver os títulos.")
    elif key == "ar-bank":
        detail = _grouped_titles_detail("Contas a receber · por banco / carteira", _group_rows_by_field(_financial_group_rows(db, cid, "RECEBER", "open"), "collection_group"), "Quando não há banco, o BI usa a carteira identificada. Sem banco e sem carteira, o título fica em Cobrança não identificada.", "Banco / carteira")
    elif key == "ar-aging":
        detail = _grouped_titles_detail("Contas a receber · aging", _aging_grouped_rows(_financial_group_rows(db, cid, "RECEBER", "open")), "Faixas calculadas sobre o saldo atual em aberto. Toque em uma faixa para ver os títulos.", "Faixa")
    elif key == "ap-open":
        detail = _grouped_financial_detail("Contas a pagar em aberto", _financial_group_rows(db, cid, "PAGAR", "open"), "Agrupado por fornecedor. Toque no fornecedor para ver os títulos.", "balance", "Fornecedor")
    elif key == "ap-overdue":
        detail = _grouped_financial_detail("Contas a pagar · vencidos", _financial_group_rows(db, cid, "PAGAR", "overdue"), "Agrupado por fornecedor. Toque no fornecedor para ver os títulos vencidos.", "balance", "Fornecedor")
    elif key == "ap-due":
        detail = _grouped_financial_detail("Contas a pagar · a vencer", _financial_group_rows(db, cid, "PAGAR", "due"), "Agrupado por fornecedor. Toque no fornecedor para ver os títulos a vencer.", "balance", "Fornecedor")
    elif key == "ap-paid":
        detail = _grouped_financial_detail("Contas a pagar · pagos", _financial_group_rows(db, cid, ["PAGAR", "DESPESA"], "paid", start, end), "Agrupado por fornecedor. Toque no fornecedor para ver os pagamentos.", "paid", "Fornecedor")
    elif key == "ap-supplier":
        detail = _grouped_financial_detail("Contas a pagar · por fornecedor", _financial_group_rows(db, cid, "PAGAR", "open"), "Agrupado por fornecedor. Toque para ver os títulos.", "balance", "Fornecedor")
    elif key == "sales-client":
        detail = _group_breakdown_with_children("Faturamento · por cliente", _sales_grouped_by_partner(db, cid, start, end), "Agrupado por cliente. Toque no cliente para ver as NF-e válidas do período.")
    elif key == "sales-zone":
        detail = _group_breakdown_with_children("Faturamento · por zona", _sales_grouped_by_zone(db, cid, start, end), "Zonas comerciais padronizadas: Sul, Leste, Norte, Oeste, ABC, Alto Tietê, Interior e Outros. Usa região logística e, quando necessário, cidade/bairro/CEP da entrega.")
    elif key == "sales-state":
        detail = _group_breakdown_with_children("Faturamento · por estado", _sales_grouped_by_state(db, cid, start, end), "Classificação por UF usando o estado de entrega/cadastro do cliente.")
    elif key == "sales-payment-practiced":
        detail = _group_breakdown_with_children("Faturamento · prazo praticado na venda", _sales_grouped_by_practiced_payment(db, cid, start, end), "Prazo efetivamente praticado: prioriza cobrança/vencimentos da NF ou pedido e, quando necessário, cruza a venda com Contas a Receber por NF-e, pedido/documento ou emissão do mesmo cliente. Sem vínculo seguro fica como não identificado.")
    elif key == "sales-terms":
        detail = _group_breakdown_with_children("Faturamento · prazo cadastrado do cliente", _sales_grouped_by_terms(db, cid, start, end), "Prazo atualmente cadastrado no cliente; pode diferir da condição realmente praticada na venda.")
    elif key == "sales-value-range":
        detail = _group_breakdown_with_children("Faturamento · por faixa de valor", _sales_grouped_by_value_range(db, cid, start, end), "Faixas calculadas sobre o valor total de cada NF-e válida do período.")
    elif key == "sales-price-audit":
        detail = _sales_price_audit(db, cid, start, end)
    elif key == "sales-period":
        detail = _group_breakdown_with_children("NF-e do período · por data", _sales_grouped_by_date(db, cid, start, end), "Agrupado por data de emissão. Toque na data para ver as NF-e.")
    elif key == "commercial-seller":
        detail = _group_breakdown_with_children("Comercial · faturamento por vendedor", _commercial_grouped_by_seller(db, cid, start, end), "Toque no vendedor para ver as NF-e do período.")
    elif key == "commercial-category":
        detail = _group_breakdown_with_children("Comercial · por categoria de produtos", _commercial_grouped_by_product(db, cid, start, end, "category"), "Valores baseados nos itens identificados das NF-e válidas. Toque na categoria para ver os produtos e as NF-e relacionadas.")
    elif key == "commercial-subcategory":
        detail = _group_breakdown_with_children("Comercial · por subcategoria de produtos", _commercial_grouped_by_product(db, cid, start, end, "subcategory"), "Valores baseados nos itens identificados das NF-e válidas. Toque na subcategoria para ver os produtos e as NF-e relacionadas.")
    elif key == "commercial-price-table":
        detail = _product_price_lookup(db, cid, query)
    elif key == "cash-period":
        return _cash_grouped(db, cid, start, end)
    elif key == "agenda-7":
        return _agenda_grouped(db, cid, 7, 100)
    elif key == "top-clients":
        return _grouped_financial_detail("Maiores clientes em aberto", _financial_group_rows(db, cid, "RECEBER", "open"), "Agrupado por cliente. Toque para ver os títulos.", "balance", "Cliente")
    elif key == "top-suppliers":
        return _grouped_financial_detail("Maiores fornecedores em aberto", _financial_group_rows(db, cid, "PAGAR", "open"), "Agrupado por fornecedor. Toque para ver os títulos.", "balance", "Fornecedor")
    else:
        return {"title": "Detalhes", "kind": "empty", "rows": []}
    return _attach_options(detail, key)

