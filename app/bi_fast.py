from __future__ import annotations

import json
import time
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from .bi_service import _is_cancelled, _period_bounds, _previous_period, _seller_info, _walk_find, _product_row_meta
from .db import (
    CollectionCustomerRule,
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
            SalesInvoice.delivery_state,
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
        select(FinancialTitle.partner_id, label.label("name"), func.sum(bal).label("value"))
        .outerjoin(Partner, Partner.id == FinancialTitle.partner_id)
        .where(FinancialTitle.company_id == cid, FinancialTitle.kind == kind, bal > 0.005)
        .group_by(FinancialTitle.partner_id, Partner.trade_name, Partner.name)
        .order_by(func.sum(bal).desc())
        .limit(limit)
    ).all()
    return [{"partner_id": pid, "name": str(name or "Sem cadastro"), "value": _money(value)} for pid, name, value in rows]


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
        ("ar-bank", "Por banco"),
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
    rule_map = {int(r.partner_id): max(0.0, min(float(r.contract_discount_percent or 0), 100.0)) for r in rules}
    if not rule_map:
        return []
    bal = _balance_expr()
    label = _partner_label_expr()
    stmt = (
        select(
            FinancialTitle.id, FinancialTitle.document, FinancialTitle.issue_date,
            FinancialTitle.due_date, FinancialTitle.paid_date, FinancialTitle.partner_id,
            label.label("partner"), FinancialTitle.value, FinancialTitle.paid_value,
            bal.label("balance"),
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




def _grouped_financial_detail(title: str, rows: list[dict], note: str = "", aggregate_on: str = "balance", partner_label: str = "Cliente/Fornecedor") -> dict:
    acc: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("partner") or "Sem cadastro")
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "titles": []})
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
        name = str(row.get("partner") or "Sem cadastro")
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "gross": 0.0, "discount": 0.0, "received": 0.0, "titles": []})
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


def _classify_zone(name: str) -> str:
    t = str(name or "").strip().lower()
    if not t:
        return "Sem zona classificada"
    if "leste" in t:
        return "Zona Leste"
    if "oeste" in t:
        return "Zona Oeste"
    if "norte" in t:
        return "Zona Norte"
    if "sul" in t:
        return "Zona Sul"
    if "centro" in t or "central" in t:
        return "Centro"
    if "abc" in t:
        return "ABC"
    if "interior" in t:
        return "Interior"
    if "litoral" in t or "baixada" in t:
        return "Litoral"
    return str(name).strip()


def _sales_invoice_rows(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    return [
        {"id": int(r.id), "number": r.number or "—", "date": r.issue_date or "—",
         "partner": ((partners.get(int(r.customer_id or 0)).trade_name or partners.get(int(r.customer_id or 0)).name) if partners.get(int(r.customer_id or 0)) else "Sem cadastro"),
         "value": _money(r.total)}
        for r in sorted(sales, key=lambda x: str(x.issue_date or ""), reverse=True)
    ]


def _sales_grouped_by_partner(db, cid: int, start: date, end: date) -> list[dict]:
    rows = _sales_invoice_rows(db, cid, start, end)
    acc: dict[str, dict] = {}
    for row in rows:
        name = row["partner"]
        cell = acc.setdefault(name, {"name": name, "count": 0, "value": 0.0, "invoices": []})
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
    delivery_rows = db.execute(select(Delivery.sales_invoice_id, Delivery.region).where(Delivery.company_id == cid, Delivery.sales_invoice_id.in_(ids))).all()
    region_by_invoice = {int(invoice_id): str(region or "").strip() for invoice_id, region in delivery_rows}
    acc: dict[str, dict] = {}
    for inv in sales:
        raw_region = region_by_invoice.get(int(inv.id)) or "Sem zona classificada"
        zone = _classify_zone(raw_region)
        partner = partners.get(int(inv.customer_id or 0))
        invoice = {"id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—", "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro", "value": _money(inv.total), "region": raw_region}
        cell = acc.setdefault(zone, {"name": zone, "count": 0, "value": 0.0, "invoices": []})
        cell["count"] += 1
        cell["value"] += _money(inv.total)
        cell["invoices"].append(invoice)
    return sorted(({**v, "value": round(v["value"], 2)} for v in acc.values()), key=lambda r: r["value"], reverse=True)


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


def _payment_term_from_payload(payload: dict, issue_raw: Any) -> tuple[str, str]:
    issue = _parse_calendar_date(issue_raw)
    detail = payload.get("__detail__") if isinstance(payload, dict) else None
    sources = [detail, payload] if isinstance(detail, dict) else [payload]
    term_keys = {
        "prazo_pagamento", "prazo_de_pagamento", "condicao_pagamento", "condicao_de_pagamento",
        "payment_terms", "payment_term", "descricao_condicao_pagamento", "forma_prazo_pagamento",
        "condicao_pgto", "prazo_pgto", "cond_pagamento"
    }
    for source in sources:
        if not isinstance(source, dict):
            continue
        for value in _iter_payload_values(source, term_keys):
            label = _label_from_value(value)
            if label and label not in {"0", "0 dias"}:
                if re.fullmatch(r"\d+(?:[.,]\d+)?", label):
                    label = f"{label} dias"
                return label, "NF/pedido"

    due_keys = {"vencimento", "data_vencimento", "dt_vencimento", "due_date", "data_vcto", "vcto"}
    due_dates: list[date] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for value in _iter_payload_values(source, due_keys):
            if isinstance(value, list):
                candidates = value
            else:
                candidates = [value]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate = candidate.get("data") or candidate.get("vencimento") or candidate.get("due_date")
                d = _parse_calendar_date(candidate)
                if d:
                    due_dates.append(d)
    if issue and due_dates:
        offsets = sorted({max((d - issue).days, 0) for d in due_dates})
        if offsets == [0]:
            return "À vista", "vencimentos da venda"
        positives = [x for x in offsets if x > 0]
        if positives:
            return "/".join(str(x) for x in positives) + " dias", "vencimentos da venda"
    return "Prazo não identificado", "indisponível"


def _sales_grouped_by_practiced_payment(db, cid: int, start: date, end: date) -> list[dict]:
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    payloads = _invoice_payment_payloads(db, cid, sales)
    acc: dict[str, dict] = {}
    for inv in sales:
        name, source = _payment_term_from_payload(payloads.get(int(inv.id), {}), inv.issue_date)
        partner = partners.get(int(inv.customer_id or 0))
        invoice = {
            "id": int(inv.id), "number": inv.number or "—", "date": inv.issue_date or "—",
            "partner": (partner.trade_name or partner.name) if partner else "Sem cadastro",
            "value": _money(inv.total), "payment_source": source,
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
    return sorted(result, key=lambda r: r["value"], reverse=True)


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
        label = str(name or "").strip()
        if num is None or num < 0 or not label:
            return
        found[(label, num)] = {"table": label, "price": num, "source": source}

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


def _financial_group_rows(db, cid: int, kind: str | list[str], status: str = "open", start: date | None = None, end: date | None = None) -> list[dict]:
    # Intentionally bounded only after grouping; this avoids a top-200 titles bias by due date.
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
        FinancialTitle.id, FinancialTitle.document, FinancialTitle.issue_date, FinancialTitle.due_date,
        FinancialTitle.paid_date, label.label("partner"), FinancialTitle.value, FinancialTitle.paid_value,
        FinancialTitle.bank, bal.label("balance")
    ).outerjoin(Partner, Partner.id == FinancialTitle.partner_id).where(*conditions)).all()
    return [{
        "id": int(r.id), "document": r.document or "—", "partner": str(r.partner or "Sem cadastro"),
        "issue": r.issue_date or "—", "due": r.due_date or "—", "paid_date": r.paid_date or "—",
        "value": _money(r.value), "paid": _money(r.paid_value), "balance": _money(r.balance),
        "bank": str(r.bank or "Sem banco").strip() or "Sem banco",
    } for r in rows]


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
        detail = _grouped_financial_detail("Contas a receber em aberto", _financial_group_rows(db, cid, "RECEBER", "open"), "Agrupado por cliente. Toque no cliente para ver os títulos.", "balance", "Cliente")
    elif key == "ar-overdue":
        detail = _grouped_financial_detail("Contas a receber · vencidos", _financial_group_rows(db, cid, "RECEBER", "overdue"), "Agrupado por cliente. Toque no cliente para ver os títulos vencidos.", "balance", "Cliente")
    elif key == "ar-due":
        detail = _grouped_financial_detail("Contas a receber · a vencer", _financial_group_rows(db, cid, "RECEBER", "due"), "Agrupado por cliente. Toque no cliente para ver os títulos a vencer.", "balance", "Cliente")
    elif key == "ar-received":
        detail = _grouped_financial_detail("Contas a receber · recebidos", _financial_group_rows(db, cid, "RECEBER", "paid", start, end), "Agrupado por cliente. Toque no cliente para ver os recebimentos.", "paid", "Cliente")
    elif key == "ar-contract":
        detail = _grouped_contract_detail("Contas a receber · contratos", _contract_title_rows(db, cid, None), "Clientes marcados como cobrança por contrato. Toque no cliente para ver os títulos.")
    elif key == "ar-bank":
        detail = _grouped_titles_detail("Contas a receber · por banco", _group_rows_by_field(_financial_group_rows(db, cid, "RECEBER", "open"), "bank"), "Agrupado por banco. Toque no banco para ver os títulos.", "Banco")
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
        detail = _group_breakdown_with_children("Faturamento · por zona", _sales_grouped_by_zone(db, cid, start, end), "Classificação por zona usando a região logística da entrega.")
    elif key == "sales-state":
        detail = _group_breakdown_with_children("Faturamento · por estado", _sales_grouped_by_state(db, cid, start, end), "Classificação por UF usando o estado de entrega/cadastro do cliente.")
    elif key == "sales-payment-practiced":
        detail = _group_breakdown_with_children("Faturamento · prazo praticado na venda", _sales_grouped_by_practiced_payment(db, cid, start, end), "Condição obtida da própria NF/pedido ou calculada pelos vencimentos encontrados na venda. Sem informação suficiente fica separado como não identificado.")
    elif key == "sales-terms":
        detail = _group_breakdown_with_children("Faturamento · prazo cadastrado do cliente", _sales_grouped_by_terms(db, cid, start, end), "Prazo atualmente cadastrado no cliente; pode diferir da condição realmente praticada na venda.")
    elif key == "sales-value-range":
        detail = _group_breakdown_with_children("Faturamento · por faixa de valor", _sales_grouped_by_value_range(db, cid, start, end), "Faixas calculadas sobre o valor total de cada NF-e válida do período.")
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

