from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from .bi_service import _is_cancelled, _period_bounds, _previous_period
from .db import (
    FinancialTitle,
    Partner,
    PurchaseInvoice,
    RawRecord,
    SalesInvoice,
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


def build_fast_detail(db, cid: int, key: str, period: str = "month", date_from: str = "", date_to: str = "") -> dict:
    start, end = _period_bounds(period, date_from, date_to)
    if key == "ar-open":
        return {"title": "Contas a receber em aberto", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "open", limit=100)}
    if key == "ar-overdue":
        return {"title": "Contas a receber vencidas", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "overdue", limit=100)}
    if key == "ar-due":
        return {"title": "Contas a receber a vencer", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "due", limit=100)}
    if key == "ar-received":
        return {"title": "Recebimentos do período", "kind": "financial", "rows": _title_rows(db, cid, "RECEBER", "paid", start, end, 100)}
    if key == "ap-open":
        return {"title": "Contas a pagar em aberto", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "open", limit=100)}
    if key == "ap-overdue":
        return {"title": "Contas a pagar vencidas", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "overdue", limit=100)}
    if key == "ap-due":
        return {"title": "Contas a pagar a vencer", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "due", limit=100)}
    if key == "ap-paid":
        return {"title": "Pagamentos do período", "kind": "financial", "rows": _title_rows(db, cid, "PAGAR", "paid", start, end, 100)}
    if key == "sales-period":
        sales = _sales_rows(db, cid, start, end)
        partner_ids = {int(r.customer_id) for r in sales if r.customer_id}
        partners = {p.id: (p.trade_name or p.name or "Sem cadastro") for p in db.scalars(select(Partner).where(Partner.id.in_(partner_ids or [-1]))).all()}
        rows = [
            {"id": int(r.id), "number": r.number or "—", "date": r.issue_date or "—", "partner": partners.get(r.customer_id, "Sem cadastro"), "value": _money(r.total)}
            for r in sorted(sales, key=lambda x: str(x.issue_date or ""), reverse=True)[:100]
        ]
        return {"title": "Faturamento do período", "kind": "sales", "rows": rows}
    if key == "cash-period":
        return {
            "title": "Movimentação financeira do período",
            "kind": "cash",
            "receivables": _title_rows(db, cid, "RECEBER", "paid", start, end, 60),
            "payables": _title_rows(db, cid, ["PAGAR", "DESPESA"], "paid", start, end, 60),
        }
    if key == "agenda-7":
        return {"title": "Agenda financeira · próximos 7 dias", "kind": "agenda", "rows": _agenda(db, cid, 7, 100)}
    if key == "top-clients":
        return {"title": "Maiores clientes em aberto", "kind": "ranking", "rows": _top_open_partners(db, cid, "RECEBER", 30)}
    if key == "top-suppliers":
        return {"title": "Maiores fornecedores em aberto", "kind": "ranking", "rows": _top_open_partners(db, cid, "PAGAR", 30)}
    return {"title": "Detalhes", "kind": "empty", "rows": []}
