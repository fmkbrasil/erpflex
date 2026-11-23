from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from .bi_service import _is_cancelled, _period_bounds, _previous_period, _seller_info, _walk_find
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
            SalesInvoice.number,
            SalesInvoice.issue_date,
            SalesInvoice.total,
            SalesInvoice.customer_id,
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
        ("sales-region", "Por região"),
        ("sales-terms", "Prazo de pagamento"),
        ("sales-period", "NF-e do período"),
    ],
    "commercial": [
        ("commercial-seller", "Por vendedor"),
        ("commercial-category", "Por categoria"),
        ("commercial-subcategory", "Por subcategoria"),
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


def _contract_title_rows(db, cid: int, limit: int = 100) -> list[dict]:
    rules = db.scalars(select(CollectionCustomerRule).where(
        CollectionCustomerRule.company_id == cid,
        CollectionCustomerRule.has_collection_contract == True,
    )).all()
    rule_map = {int(r.partner_id): max(0.0, min(float(r.contract_discount_percent or 0), 100.0)) for r in rules}
    if not rule_map:
        return []
    bal = _balance_expr()
    label = _partner_label_expr()
    rows = db.execute(
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
        .limit(limit)
    ).all()
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
    sales = _sales_rows(db, cid, start, end)
    partners = _partner_map(db, {int(r.customer_id) for r in sales if r.customer_id})
    rows = [
        {"id": int(r.id), "number": r.number or "—", "date": r.issue_date or "—",
         "partner": ((partners.get(int(r.customer_id or 0)).trade_name or partners.get(int(r.customer_id or 0)).name) if partners.get(int(r.customer_id or 0)) else "Sem cadastro"),
         "value": _money(r.total)}
        for r in sorted(sales, key=lambda x: str(x.issue_date or ""), reverse=True)[:100]
    ]
    return {"title": "NF-e do período", "kind": "sales", "rows": rows}


def build_fast_detail(db, cid: int, key: str, period: str = "month", date_from: str = "", date_to: str = "") -> dict:
    start, end = _period_bounds(period, date_from, date_to)
    key = ROOT_DETAIL.get(key, key)

    if key == "ar-open":
        detail = {"title": "Contas a receber em aberto", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "open", limit=100)}
    elif key == "ar-overdue":
        detail = {"title": "Contas a receber · vencidos", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "overdue", limit=100)}
    elif key == "ar-due":
        detail = {"title": "Contas a receber · a vencer", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "due", limit=100)}
    elif key == "ar-received":
        detail = {"title": "Contas a receber · recebidos", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "paid", start, end, 100)}
    elif key == "ar-contract":
        detail = {"title": "Contas a receber · contratos", "kind": "contract_financial", "rows": _contract_title_rows(db, cid, 100), "note": "Somente títulos em aberto de clientes marcados como cobrança por contrato."}
    elif key == "ar-bank":
        detail = _breakdown("Contas a receber · por banco", _bank_breakdown(db, cid), "Posição atual dos títulos em aberto.")
    elif key == "ar-aging":
        detail = _breakdown("Contas a receber · aging", _aging_breakdown(db, cid), "Faixas calculadas sobre o saldo atual em aberto.")
    elif key == "ap-open":
        detail = {"title": "Contas a pagar em aberto", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "open", limit=100)}
    elif key == "ap-overdue":
        detail = {"title": "Contas a pagar · vencidos", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "overdue", limit=100)}
    elif key == "ap-due":
        detail = {"title": "Contas a pagar · a vencer", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "due", limit=100)}
    elif key == "ap-paid":
        detail = {"title": "Contas a pagar · pagos", "kind": "financial", "rows": _title_rows(db, cid, ["PAGAR", "DESPESA"], "paid", start, end, 100)}
    elif key == "ap-supplier":
        detail = _breakdown("Contas a pagar · por fornecedor", _supplier_breakdown(db, cid), "Posição atual dos títulos em aberto.")
    elif key == "sales-client":
        detail = _breakdown("Faturamento · por cliente", _sales_client_breakdown(db, cid, start, end), "Somente NF-e válidas do período e fora dos CFOPs excluídos.")
    elif key == "sales-region":
        detail = _breakdown("Faturamento · por região", _sales_region_breakdown(db, cid, start, end), "Região logística vinculada à NF-e. Notas sem classificação aparecem separadamente.")
    elif key == "sales-terms":
        detail = _breakdown("Faturamento · por prazo de pagamento", _sales_terms_breakdown(db, cid, start, end), "Prazo atualmente cadastrado para o cliente da NF-e.")
    elif key == "sales-period":
        detail = _sales_invoice_detail(db, cid, start, end)
    elif key == "commercial-seller":
        detail = _breakdown("Comercial · faturamento por vendedor", _seller_breakdown(db, cid, start, end), "Vendedor obtido do faturamento/pedido sincronizado; não identificado permanece separado.")
    elif key == "commercial-category":
        detail = _breakdown("Comercial · por categoria de produtos", _product_breakdown(db, cid, start, end, "category"), "Faturamento dos itens das NF-e válidas do período.")
    elif key == "commercial-subcategory":
        detail = _breakdown("Comercial · por subcategoria de produtos", _product_breakdown(db, cid, start, end, "subcategory"), "Faturamento dos itens das NF-e válidas do período.")
    elif key == "cash-period":
        return {
            "title": "Movimentação financeira do período",
            "kind": "cash",
            "receivables": _title_rows(db, cid, "RECEBER", "paid", start, end, 60),
            "payables": _title_rows(db, cid, ["PAGAR", "DESPESA"], "paid", start, end, 60),
        }
    elif key == "agenda-7":
        return {"title": "Agenda financeira · próximos 7 dias", "kind": "agenda", "rows": _agenda(db, cid, 7, 100)}
    elif key == "top-clients":
        return {"title": "Maiores clientes em aberto", "kind": "ranking", "rows": _top_open_partners(db, cid, "RECEBER", 30)}
    elif key == "top-suppliers":
        return {"title": "Maiores fornecedores em aberto", "kind": "ranking", "rows": _top_open_partners(db, cid, "PAGAR", 30)}
    else:
        return {"title": "Detalhes", "kind": "empty", "rows": []}
    return _attach_options(detail, key)

