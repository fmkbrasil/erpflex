from __future__ import annotations

import json
import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from .db import (
    Delivery, DreExpenseMapping, FinancialTitle, Partner, Product, PurchaseInvoice, PurchaseItem,
    RawRecord, SalesInvoice, SalesInvoiceItem, SalesOrder, SyncRun,
)
from .utils import VERIFIED_BANK_NAMES


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _pct(num: float, den: float) -> float | None:
    if abs(den) < 0.000001:
        return None
    return round(num / den * 100.0, 1)


def _variation(current: float, previous: float) -> float | None:
    if abs(previous) < 0.000001:
        return None if abs(current) < 0.000001 else 100.0
    return round((current - previous) / abs(previous) * 100.0, 1)


def _period_bounds(period: str, date_from: str = "", date_to: str = "") -> tuple[date, date]:
    today = date.today()
    if period == "custom":
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
            return (min(start, end), max(start, end))
        except Exception:
            pass
    if period == "today":
        return today, today
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "7":
        return today - timedelta(days=6), today
    if period == "30":
        return today - timedelta(days=29), today
    if period == "prev_month":
        first = date(today.year, today.month, 1)
        end = first - timedelta(days=1)
        return date(end.year, end.month, 1), end
    if period == "quarter":
        month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, month, 1), today
    if period == "year":
        return date(today.year, 1, 1), today
    return date(today.year, today.month, 1), today


def _previous_period(start: date, end: date) -> tuple[date, date]:
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=days - 1), prev_end


def _year_ago(start: date, end: date) -> tuple[date, date]:
    def shift(d: date) -> date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            return d.replace(year=d.year - 1, day=28)
    return shift(start), shift(end)


def _in_period(value: Any, start: date, end: date) -> bool:
    d = _date(value)
    return bool(d and start <= d <= end)


def _raw_payload(raw_map: dict[int, RawRecord], raw_id: int | None) -> dict:
    raw = raw_map.get(raw_id or -1)
    if not raw or not raw.payload_json:
        return {}
    try:
        value = json.loads(raw.payload_json)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _walk_find(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        lower = {str(k).lower(): v for k, v in value.items()}
        for key in keys:
            if key in lower and lower[key] not in (None, "", []):
                return lower[key]
        for v in value.values():
            found = _walk_find(v, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for v in value:
            found = _walk_find(v, keys)
            if found not in (None, "", []):
                return found
    return None


def _seller_info(payload: dict | None) -> dict:
    """Normaliza vendedor/representante sem supor um nome único de campo na API."""
    payload = payload or {}
    seller_id = _walk_find(payload, {
        "id_vendedor", "vendedor_id", "idvendedor", "idrepresentante",
        "id_representante", "representante_id", "seller_id", "salesperson_id",
    })
    seller_name = _walk_find(payload, {
        "vendedor_nome", "nome_vendedor", "vendedor_desc", "descricao_vendedor", "nomevendedor",
        "representante_nome", "nome_representante", "representante_desc", "descricao_representante",
        "seller_name", "salesperson_name",
    })
    nested = _walk_find(payload, {"vendedor", "representante", "seller", "salesperson"})
    if isinstance(nested, dict):
        if seller_id in (None, "", []):
            seller_id = _walk_find(nested, {"id", "codigo", "code", "vendedor_id", "id_vendedor"})
        if seller_name in (None, "", []):
            seller_name = _walk_find(nested, {"nome", "name", "descricao", "description", "razao_social"})
    elif nested not in (None, "", []):
        raw = str(nested).strip()
        if raw:
            if raw.replace(".", "", 1).isdigit():
                seller_id = seller_id or raw
            else:
                seller_name = seller_name or raw

    def scan(node: Any):
        nonlocal seller_id, seller_name
        if isinstance(node, dict):
            for key, value in node.items():
                lk = str(key).lower()
                if "vendedor" in lk or "representante" in lk:
                    if isinstance(value, dict):
                        if seller_id in (None, "", []):
                            seller_id = _walk_find(value, {"id", "codigo", "code"})
                        if seller_name in (None, "", []):
                            seller_name = _walk_find(value, {"nome", "name", "descricao", "description"})
                    elif value not in (None, "", [], {}):
                        raw = str(value).strip()
                        if raw:
                            if any(token in lk for token in ("id", "codigo", "code")) or raw.replace(".", "", 1).isdigit():
                                seller_id = seller_id or raw
                            else:
                                seller_name = seller_name or raw
                if not (seller_id not in (None, "", []) and seller_name not in (None, "", [])):
                    scan(value)
        elif isinstance(node, list):
            for value in node:
                if seller_id not in (None, "", []) and seller_name not in (None, "", []):
                    break
                scan(value)
    if not (seller_id not in (None, "", []) and seller_name not in (None, "", [])):
        scan(payload)
    sid = str(seller_id or "").strip()[:120]
    name = str(seller_name or "").strip()[:180]
    display = name or (f"Vendedor ID {sid}" if sid else "")
    return {"id": sid or None, "name": name or None, "display": display or None}


def _seller(payload: dict | None) -> str | None:
    return _seller_info(payload).get("display")


def _generic_bank(value: Any) -> bool:
    txt = str(value or "").strip().lower()
    return (not txt) or txt.startswith("banco ") or txt.startswith("sem banco") or txt.startswith("banco id ")


def _bank_catalog(raw_records: list[RawRecord], raw_map: dict[int, RawRecord]) -> dict[str, str]:
    """Catálogo de bancos a partir de todos os registros persistidos.

    A versão anterior podia deixar ``Banco 25325`` no BI mesmo quando o cadastro
    sincronizado já tinha SA6_Desc. Aqui cada nó é percorrido individualmente.
    """
    out: dict[str, str] = dict(VERIFIED_BANK_NAMES)

    def as_text(value: Any) -> str:
        if isinstance(value, dict):
            value = _walk_find(value, {"nome", "name", "descricao", "description", "codigo", "id"})
        return str(value or "").strip()

    def walk(node: Any, detail_external: str = ""):
        if isinstance(node, dict):
            bid = as_text(_walk_find_shallow(node, {"sa6_id", "id_banco", "banco_id", "idbanco"}))
            code = as_text(_walk_find_shallow(node, {"sa6_codigo", "codigo_banco", "codigobanco"}))
            name = as_text(_walk_find_shallow(node, {"sa6_desc", "banco_desc", "nome_banco", "descricao_banco"}))
            if not bid and detail_external and name:
                bid = detail_external
            if bid and not name:
                generic = _walk_find_shallow(node, {"nome", "descricao", "banco"})
                name = as_text(generic)
            if bid and name and not _generic_bank(name):
                out[bid] = name
            if code and name and not _generic_bank(name):
                out.setdefault(code, name)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child, detail_external)
        elif isinstance(node, list):
            for child in node:
                walk(child, detail_external)

    for raw in raw_records:
        if str(raw.source or "").upper() != "ERPFLEX" or str(raw.module or "").lower() not in {"banks", "bank_detail"}:
            continue
        payload = _raw_payload(raw_map, raw.id)
        if payload:
            walk(payload, str(raw.external_id or "").strip() if str(raw.module or "").lower() == "bank_detail" else "")
    return out


def _walk_find_shallow(node: Any, keys: set[str]) -> Any:
    if not isinstance(node, dict):
        return None
    lower = {str(k).lower(): v for k, v in node.items()}
    for key in keys:
        if key in lower and lower[key] not in (None, "", [], {}):
            return lower[key]
    return None


def _bank_info(title: FinancialTitle, raw_map: dict[int, RawRecord], detail_by_external: dict[str, dict], catalog: dict[str, str]) -> dict:
    payload = dict(_raw_payload(raw_map, title.raw_record_id) or {})
    detail = detail_by_external.get(str(title.erpflex_id or ""), {}) if title.kind == "RECEBER" else {}
    if detail:
        payload.update(detail)
    bank_id = str(_walk_find(payload, {"id_banco", "banco_id", "idbanco", "sa6_id"}) or "").strip()
    stored = str(title.bank or "").strip()
    if not bank_id and stored.lower().startswith("banco "):
        possible = stored.split(" ", 1)[1].strip()
        if possible:
            bank_id = possible
    desc = _walk_find(payload, {"banco_desc", "nome_banco", "descricao_banco", "sa6_desc"})
    if isinstance(desc, dict):
        desc = desc.get("nome") or desc.get("name") or desc.get("descricao")
    desc_text = str(desc or "").strip()
    stored_specific = "" if _generic_bank(stored) else stored
    label = desc_text or stored_specific or catalog.get(bank_id, "")
    if not label:
        label = "Sem banco" if bank_id in {"", "0"} else f"Banco {bank_id}"
    return {"id": bank_id or None, "name": label}


def _generic_wallet(value: Any) -> bool:
    txt = str(value or "").strip().lower()
    return (not txt) or txt.startswith("carteira ") or txt.startswith("sem carteira") or txt.startswith("carteira não")


def _wallet_catalog(raw_records: list[RawRecord], raw_map: dict[int, RawRecord]) -> dict[tuple[str, str], str]:
    """Monta catálogo ID -> descrição com retornos já persistidos de bancos/opções de boleto."""
    out: dict[tuple[str, str], str] = {}

    def local(node: dict, names: tuple[str, ...]):
        lower = {str(k).lower(): v for k, v in node.items()}
        for name in names:
            if name.lower() in lower and lower[name.lower()] not in (None, "", []):
                return lower[name.lower()]
        return None

    def as_text(value: Any) -> str:
        if isinstance(value, dict):
            value = local(value, ("nome", "name", "descricao", "description", "label", "text"))
        return str(value or "").strip()

    def walk(node: Any, bank_ctx: str = "", wallet_ctx: bool = False):
        if isinstance(node, dict):
            bank_here = as_text(local(node, ("id_banco", "banco_id", "idBanco", "sa6_id"))) or bank_ctx
            wid = as_text(local(node, ("id_carteira", "carteira_id", "idCarteira", "wallet_id")))
            label = as_text(local(node, ("carteira_desc", "nome_carteira", "descricao_carteira", "wallet_name", "label", "text")))
            if wid and label and label != wid and not _generic_wallet(label):
                out[(bank_here, wid)] = label
                out.setdefault(("", wid), label)
            for key, child in node.items():
                lk = str(key).lower()
                walk(child, bank_here, wallet_ctx or "carteir" in lk or "wallet" in lk)
        elif isinstance(node, list):
            for child in node:
                walk(child, bank_ctx, wallet_ctx)

    for raw in raw_records:
        if str(raw.source or "").upper() != "ERPFLEX" or str(raw.module or "").lower() not in {"banks", "bank_detail", "boleto_options"}:
            continue
        payload = _raw_payload(raw_map, raw.id)
        if payload:
            walk(payload)
    return out


def _wallet_info(title: FinancialTitle, raw_map: dict[int, RawRecord], detail_by_external: dict[str, dict], catalog: dict[tuple[str, str], str]) -> dict:
    payload = dict(_raw_payload(raw_map, title.raw_record_id) or {})
    detail = detail_by_external.get(str(title.erpflex_id or ""), {})
    if detail:
        payload.update(detail)
    bank_id = str(_walk_find(payload, {"id_banco", "banco_id", "idbanco"}) or "").strip()
    wallet_id = str(_walk_find(payload, {"id_carteira", "carteira_id", "idcarteira", "wallet_id"}) or "").strip()
    desc = _walk_find(payload, {"carteira_desc", "nome_carteira", "descricao_carteira", "wallet_name"})
    if isinstance(desc, dict):
        desc = desc.get("nome") or desc.get("name") or desc.get("descricao") or desc.get("label")
    desc_text = str(desc or "").strip()
    stored = str(title.wallet or "").strip()
    if not wallet_id and stored.lower().startswith("carteira "):
        possible = stored.split(" ", 1)[1].strip()
        if possible and possible.lower() not in {"não informada", "nao informada"}:
            wallet_id = possible
    stored_specific = "" if _generic_wallet(stored) else stored
    label = desc_text or stored_specific or catalog.get((bank_id, wallet_id), "") or catalog.get(("", wallet_id), "")
    if not label:
        label = "Sem carteira" if wallet_id in {"", "0"} else f"Carteira {wallet_id}"
    return {"id": wallet_id or None, "bank_id": bank_id or None, "name": label}


DRE_GROUPS = {
    "COMERCIAL": "Despesas Comerciais",
    "ADMINISTRATIVA": "Despesas Administrativas",
    "LOGISTICA": "Despesas Logísticas",
    "OUTRAS_OPERACIONAIS": "Outras Despesas Operacionais",
    "FINANCEIRA": "Despesas Financeiras",
    "NAO_CLASSIFICADO": "Não classificado",
}


def _expense_category(payload: dict | None) -> str:
    """Extrai a categoria real da despesa sem inventar classificação contábil."""
    payload = payload or {}
    value = None
    for key in (
        "categoria_despesa", "categoria", "grupo_despesa", "grupo",
        "classificacao_despesa", "classificacao", "natureza_despesa", "natureza",
        "tipo_despesa", "centro_de_custo", "centro_custo", "plano_conta", "conta",
    ):
        value = _walk_find(payload, {key})
        if value not in (None, "", []):
            break
    if isinstance(value, dict):
        value = value.get("nome") or value.get("descricao") or value.get("name") or value.get("description") or value.get("codigo")
    text = str(value or "").strip()
    return text[:255] if text else "Sem categoria informada"


def _dre_group_label(code: str | None) -> str:
    return DRE_GROUPS.get(str(code or "NAO_CLASSIFICADO"), DRE_GROUPS["NAO_CLASSIFICADO"])


def _channel(payload: dict) -> str | None:
    value = _walk_find(payload, {"canal", "canal_venda", "canal_vendas", "sales_channel"})
    if isinstance(value, dict):
        value = value.get("nome") or value.get("name") or value.get("descricao")
    text = str(value or "").strip()
    return text[:120] or None


def _is_cancelled(payload: dict) -> bool:
    val = _walk_find(payload, {"cancelado", "cancelada", "canceled", "cancelled", "is_cancelled"})
    if isinstance(val, bool):
        return val
    if str(val or "").strip().lower() in {"1", "true", "sim", "s", "yes"}:
        return True
    status = str(_walk_find(payload, {"status", "situacao", "situação", "status_nf", "situacao_nf"}) or "").lower()
    return "cancel" in status


def _partner_name(partners: dict[int, Partner], pid: int | None) -> str:
    p = partners.get(pid or -1)
    if not p:
        return "Sem cadastro"
    return (p.trade_name or p.name or f"ID {p.id}").strip()


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _specific_nf_from_payload(payload: dict | None) -> str:
    """Número fiscal real; não usa documento/pedido como fallback."""
    raw = str(_walk_find(payload or {}, {
        "nr_nfe", "nfe", "numero_nfe", "numero_nf", "numero_nota",
        "nota_fiscal", "sf2_nrnfe", "nf"
    }) or "").strip()
    if not raw or not raw.isdigit() or int(raw or "0") <= 0:
        return ""
    return raw


def _shift_month(d: date, months: int = -1) -> date:
    month_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _product_row_meta(product: Product | None, code: Any, description: Any, raw_payload: dict | None = None) -> dict:
    code_text = str(code or (product.code if product else "") or "").strip()
    name_text = str((product.description if product else "") or description or "Sem descrição").strip()
    category = str((product.category if product else "") or "").strip()
    subcategory = str((getattr(product, "subcategory", None) if product else "") or "").strip()
    payload = raw_payload or {}
    if not category and payload:
        value = _walk_find(payload, {"categoria", "grupo", "categoria_produto"})
        if isinstance(value, dict):
            value = value.get("nome") or value.get("descricao") or value.get("name")
        category = str(value or "").strip()
    if not subcategory and payload:
        value = _walk_find(payload, {"subcategoria", "sub_categoria", "subcategoria_produto", "subgrupo", "sub_grupo", "familia", "linha"})
        if isinstance(value, dict):
            value = value.get("nome") or value.get("descricao") or value.get("name")
        subcategory = str(value or "").strip()
    return {
        "code": code_text or "Sem código",
        "name": name_text or "Sem descrição",
        "category": category or "Sem categoria",
        "subcategory": subcategory or "Sem subcategoria",
    }


def _rank(sums: dict[Any, float], label_fn=lambda k: str(k), limit: int = 20) -> list[dict]:
    return [
        {"key": k, "name": label_fn(k), "value": round(v, 2)}
        for k, v in sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    ]


def _abc(rows: list[dict], a_cut: float = 80.0, b_cut: float = 95.0) -> list[dict]:
    total = sum(float(r.get("value") or 0) for r in rows)
    running = 0.0
    out = []
    for row in rows:
        value = float(row.get("value") or 0)
        share = (value / total * 100.0) if total else 0.0
        running += share
        cls = "A" if running <= a_cut or len(out) == 0 else ("B" if running <= b_cut else "C")
        out.append({**row, "share": round(share, 1), "cumulative": round(running, 1), "class": cls})
    return out


def _aging(titles: list[FinancialTitle], today: date) -> list[dict]:
    labels = ["A vencer", "1 a 7 dias", "8 a 15 dias", "16 a 30 dias", "31 a 60 dias", "61 a 90 dias", "Acima de 90 dias"]
    data = {k: {"value": 0.0, "count": 0} for k in labels}
    for t in titles:
        balance = max(_money(t.value) - _money(t.paid_value), 0.0)
        if balance <= 0.005:
            continue
        due = _date(t.due_date)
        if not due or due >= today:
            key = "A vencer"
        else:
            days = (today - due).days
            if days <= 7: key = "1 a 7 dias"
            elif days <= 15: key = "8 a 15 dias"
            elif days <= 30: key = "16 a 30 dias"
            elif days <= 60: key = "31 a 60 dias"
            elif days <= 90: key = "61 a 90 dias"
            else: key = "Acima de 90 dias"
        data[key]["value"] += balance
        data[key]["count"] += 1
    total = sum(v["value"] for v in data.values())
    return [{"name": k, "value": round(v["value"],2), "count": v["count"], "share": round(v["value"] / total * 100, 1) if total else 0.0} for k,v in data.items()]


def _forecast(titles: list[FinancialTitle], today: date, days_list=(7,15,30,60,90)) -> list[dict]:
    out=[]
    for days in days_list:
        limit=today+timedelta(days=days)
        value=0.0
        count=0
        for t in titles:
            bal=max(_money(t.value)-_money(t.paid_value),0)
            due=_date(t.due_date)
            if bal>0.005 and due and today <= due <= limit:
                value += bal; count += 1
        out.append({"days":days,"value":round(value,2),"count":count})
    return out


def _latest_sync(db, cid: int) -> datetime | None:
    run = db.scalar(select(SyncRun).where(SyncRun.company_id==cid, SyncRun.status.in_(["CONCLUIDO","SUCESSO","OK"])).order_by(SyncRun.finished_at.desc()).limit(1))
    return (run.finished_at or run.started_at) if run else None


@dataclass
class BIContext:
    start: date
    end: date
    previous_start: date
    previous_end: date
    year_start: date
    year_end: date
    period: str


def build_bi(db, cid: int, period: str="month", date_from: str="", date_to: str="", filters: dict[str,str] | None=None) -> dict:
    filters = filters or {}
    start,end = _period_bounds(period,date_from,date_to)
    ps,pe = _previous_period(start,end)
    ys,ye = _year_ago(start,end)
    ctx=BIContext(start,end,ps,pe,ys,ye,period)
    today=date.today()

    partners_list=db.scalars(select(Partner).where(Partner.company_id==cid)).all()
    partners={p.id:p for p in partners_list}
    products_list=db.scalars(select(Product).where(Product.company_id==cid)).all()
    products_by_code={str(p.code or "").strip():p for p in products_list if str(p.code or "").strip()}
    products_by_erpflex={str(p.erpflex_id or "").strip():p for p in products_list if str(p.erpflex_id or "").strip()}
    raw_records=db.scalars(select(RawRecord).where(RawRecord.company_id==cid)).all()
    raw_map={r.id:r for r in raw_records}
    product_raw_by_code={}
    product_raw_by_external={}
    for raw in raw_records:
        if str(raw.source or "").upper() != "ERPFLEX" or str(raw.module or "").lower() != "products":
            continue
        payload=_raw_payload(raw_map, raw.id)
        if not payload:
            continue
        product_raw_by_external[str(raw.external_id or "").strip()] = payload
        raw_code=_walk_find(payload, {"codigo", "codigo_produto", "sku", "referencia"})
        if raw_code not in (None, ""):
            product_raw_by_code[str(raw_code).strip()] = payload

    def product_meta(code: Any, description: Any, product_id: Any = None) -> dict:
        code_text=str(code or "").strip()
        pid_text=str(product_id or "").strip()
        product=products_by_code.get(code_text) or products_by_erpflex.get(pid_text)
        payload=product_raw_by_code.get(code_text) or product_raw_by_external.get(pid_text) or (product_raw_by_external.get(str(product.erpflex_id or "").strip()) if product else None)
        return _product_row_meta(product, code_text, description, payload)
    sales_all=db.scalars(select(SalesInvoice).where(SalesInvoice.company_id==cid)).all()
    orders_all=db.scalars(select(SalesOrder).where(SalesOrder.company_id==cid)).all()
    purchases_all=db.scalars(select(PurchaseInvoice).where(PurchaseInvoice.company_id==cid)).all()
    titles_all=db.scalars(select(FinancialTitle).where(FinancialTitle.company_id==cid)).all()
    deliveries_all=db.scalars(select(Delivery).where(Delivery.company_id==cid)).all()

    receivable_detail_by_external: dict[str, dict] = {}
    for raw in raw_records:
        if str(raw.source or "").upper() == "ERPFLEX" and str(raw.module or "").lower() == "receber_detalhe":
            payload = _raw_payload(raw_map, raw.id)
            if payload:
                receivable_detail_by_external[str(raw.external_id or "").strip()] = payload
    wallet_catalog = _wallet_catalog(raw_records, raw_map)
    bank_catalog = _bank_catalog(raw_records, raw_map)

    # O vendedor é obtido do JSON fiscal quando estiver presente; caso contrário,
    # usa somente vínculo EXATO com o pedido sincronizado (ID ou número), sem inferência por cliente.
    order_seller_by_id: dict[str, str] = {}
    order_seller_by_number: dict[str, list[str]] = defaultdict(list)
    for order in orders_all:
        raw_info = _seller_info(_raw_payload(raw_map, order.raw_record_id))
        stored_name = str(getattr(order, "seller_name", None) or "").strip()
        stored_id = str(getattr(order, "seller_id", None) or "").strip()
        seller_display = stored_name or raw_info.get("name") or (f"Vendedor ID {stored_id or raw_info.get('id')}" if (stored_id or raw_info.get("id")) else "")
        if not seller_display:
            continue
        oid = str(order.erpflex_id or "").strip()
        onum = str(order.number or "").strip()
        if oid:
            order_seller_by_id[oid] = seller_display
        if onum and seller_display not in order_seller_by_number[onum]:
            order_seller_by_number[onum].append(seller_display)

    def invoice_seller(inv: SalesInvoice) -> tuple[str | None, str]:
        payload = _raw_payload(raw_map, inv.raw_record_id)
        direct = _seller_info(payload).get("display")
        if direct:
            return direct, "faturamento/pedido detalhado"
        order_id = str(_walk_find(payload, {
            "orcamento_id", "id_orcamento", "pedido_id", "id_pedido",
            "solicitacao_id", "id_solicitacao"
        }) or "").strip()
        if order_id and order_id in order_seller_by_id:
            return order_seller_by_id[order_id], "pedido vinculado por ID"
        order_number = str(_walk_find(payload, {
            "numero_pedido", "pedido_numero", "numero_orcamento", "documento"
        }) or "").strip()
        candidates = order_seller_by_number.get(order_number, []) if order_number else []
        if len(candidates) == 1:
            return candidates[0], "pedido vinculado por número"
        return None, "não identificado"

    # Índices auxiliares para recuperar cliente e vendedor dos títulos a receber sem
    # depender exclusivamente do partner_id normalizado. A resolução é determinística:
    # cadastro explícito -> ID/CNPJ/nome no título -> NF vinculada -> pedido vinculado.
    partners_by_erpflex = {str(p.erpflex_id or "").strip(): p.id for p in partners_list if str(p.erpflex_id or "").strip()}
    partners_by_cnpj = {_digits(p.cnpj_cpf): p.id for p in partners_list if _digits(p.cnpj_cpf)}
    partners_by_name: dict[str, int] = {}
    duplicate_partner_names: set[str] = set()
    for p in partners_list:
        for value in (p.name, p.trade_name):
            key = _norm_name(value)
            if not key:
                continue
            if key in partners_by_name and partners_by_name[key] != p.id:
                duplicate_partner_names.add(key)
            else:
                partners_by_name[key] = p.id
    for key in duplicate_partner_names:
        partners_by_name.pop(key, None)

    invoices_by_number: dict[str, SalesInvoice] = {}
    invoices_by_erpflex: dict[str, SalesInvoice] = {}
    duplicated_invoice_numbers: set[str] = set()
    for inv in sales_all:
        num = str(inv.number or "").strip()
        if num:
            if num in invoices_by_number and invoices_by_number[num].id != inv.id:
                duplicated_invoice_numbers.add(num)
            else:
                invoices_by_number[num] = inv
        eid = str(inv.erpflex_id or "").strip()
        if eid:
            invoices_by_erpflex[eid] = inv
    for num in duplicated_invoice_numbers:
        invoices_by_number.pop(num, None)

    orders_by_id = {str(o.erpflex_id or "").strip(): o for o in orders_all if str(o.erpflex_id or "").strip()}
    orders_by_number: dict[str, SalesOrder] = {}
    duplicated_order_numbers: set[str] = set()
    for order in orders_all:
        num = str(order.number or "").strip()
        if not num:
            continue
        if num in orders_by_number and orders_by_number[num].id != order.id:
            duplicated_order_numbers.add(num)
        else:
            orders_by_number[num] = order
    for num in duplicated_order_numbers:
        orders_by_number.pop(num, None)

    customer_seller_by_partner: dict[int, tuple[str, str]] = {}
    for raw in raw_records:
        if str(raw.source or "").upper() != "ERPFLEX" or str(raw.module or "").lower() not in {"clientes", "cliente_detalhe"}:
            continue
        payload = _raw_payload(raw_map, raw.id)
        if not payload:
            continue
        cid_raw = str(_walk_find(payload, {"cliente_id", "id_cliente", "codigo_cliente", "codigo", "id"}) or raw.external_id or "").strip()
        pid = partners_by_erpflex.get(cid_raw)
        if not pid:
            cnpj = _digits(_walk_find(payload, {"cliente_cnpj", "cnpj_cliente", "cpf_cnpj", "cnpj_cpf", "cnpjcpf", "cnpj"}))
            pid = partners_by_cnpj.get(cnpj) if cnpj else None
        if not pid:
            name = _norm_name(_walk_find(payload, {"cliente_desc", "cliente_nome", "cliente", "razao_social", "nome_cliente", "nome"}))
            pid = partners_by_name.get(name) if name else None
        seller = _seller_info(payload).get("display")
        if pid and seller:
            customer_seller_by_partner[pid] = (seller, "cadastro atual do cliente")

    def receivable_payload(t: FinancialTitle) -> dict:
        payload = dict(_raw_payload(raw_map, t.raw_record_id) or {})
        detail = receivable_detail_by_external.get(str(t.erpflex_id or "").strip(), {})
        if detail:
            payload.update(detail)
        return payload

    def receivable_invoice(t: FinancialTitle, payload: dict | None = None) -> tuple[SalesInvoice | None, str]:
        payload = payload or receivable_payload(t)
        fid = str(_walk_find(payload, {"faturamento_id", "id_nota_saida", "id_faturamento"}) or "").strip()
        if fid and fid in invoices_by_erpflex:
            return invoices_by_erpflex[fid], "faturamento vinculado por ID"
        nf = _specific_nf_from_payload(payload)
        if nf and nf in invoices_by_number:
            return invoices_by_number[nf], "NF-e vinculada"
        return None, ""

    def receivable_order(t: FinancialTitle, payload: dict | None = None) -> tuple[SalesOrder | None, str]:
        payload = payload or receivable_payload(t)
        oid = str(_walk_find(payload, {
            "orcamento_id", "id_orcamento", "pedido_id", "id_pedido",
            "solicitacao_id", "id_solicitacao"
        }) or "").strip()
        if oid and oid in orders_by_id:
            return orders_by_id[oid], "pedido vinculado por ID"
        onum = str(_walk_find(payload, {"numero_pedido", "pedido_numero", "numero_orcamento", "documento"}) or "").strip()
        if onum and onum in orders_by_number:
            return orders_by_number[onum], "pedido vinculado por número"
        return None, ""

    resolved_partner_by_title: dict[int, int | None] = {}
    partner_label_by_title: dict[int, str] = {}
    partner_source_by_title: dict[int, str] = {}
    seller_by_title: dict[int, str | None] = {}
    seller_source_by_title: dict[int, str] = {}
    invoice_by_title: dict[int, SalesInvoice | None] = {}

    for t in titles_all:
        if t.kind != "RECEBER":
            continue
        payload = receivable_payload(t)
        pid = t.partner_id if t.partner_id in partners else None
        existing = partners.get(pid or -1)
        if existing and _norm_name(existing.name) in {"nao identificado", "não identificado", "sem cadastro"}:
            pid = None

        source = "cadastro normalizado" if pid else ""
        if not pid:
            customer_id = str(_walk_find(payload, {"cliente_id", "id_cliente", "codigo_cliente", "idcliente"}) or "").strip()
            if customer_id and customer_id in partners_by_erpflex:
                pid = partners_by_erpflex[customer_id]; source = "ID do cliente no título"
        if not pid:
            cnpj = _digits(_walk_find(payload, {"cliente_cnpj", "cnpj_cliente", "cpf_cnpj", "cnpj_cpf", "cnpjcpf", "documento_cliente"}))
            if cnpj and cnpj in partners_by_cnpj:
                pid = partners_by_cnpj[cnpj]; source = "CNPJ/CPF do cliente no título"
        if not pid:
            customer_name = _norm_name(_walk_find(payload, {"cliente_desc", "cliente_nome", "cliente", "razao_social", "nome_cliente"}))
            if customer_name and customer_name in partners_by_name:
                pid = partners_by_name[customer_name]; source = "nome único do cliente no título"

        linked_inv, inv_source = receivable_invoice(t, payload)
        invoice_by_title[t.id] = linked_inv
        if not pid and linked_inv and linked_inv.customer_id in partners:
            pid = linked_inv.customer_id; source = inv_source

        linked_order, order_source = receivable_order(t, payload)
        if not pid and linked_order and linked_order.customer_id in partners:
            pid = linked_order.customer_id; source = order_source

        resolved_partner_by_title[t.id] = pid
        if pid and pid in partners:
            partner_label_by_title[t.id] = _partner_name(partners, pid)
        else:
            raw_name = str(_walk_find(payload, {"cliente_desc", "cliente_nome", "cliente", "razao_social", "nome_cliente"}) or "").strip()
            raw_id = str(_walk_find(payload, {"cliente_id", "id_cliente", "codigo_cliente", "idcliente"}) or "").strip()
            partner_label_by_title[t.id] = raw_name or (f"Cliente ID {raw_id}" if raw_id else "Não identificado")
            source = source or ("nome retornado pela API" if raw_name else "não identificado")
        partner_source_by_title[t.id] = source or "não identificado"

        direct_seller = _seller_info(payload).get("display")
        seller = direct_seller
        seller_source = "título/detalhe financeiro" if direct_seller else ""
        if not seller and linked_inv:
            seller, seller_source = invoice_seller(linked_inv)
        if not seller and linked_order:
            stored = str(linked_order.seller_name or "").strip()
            raw_order_seller = _seller_info(_raw_payload(raw_map, linked_order.raw_record_id)).get("display")
            seller = stored or raw_order_seller
            if seller:
                seller_source = order_source or "pedido vinculado"
        if not seller and pid and pid in customer_seller_by_partner:
            seller, seller_source = customer_seller_by_partner[pid]
        seller_by_title[t.id] = seller
        seller_source_by_title[t.id] = seller_source or "não identificado"

    customer_filter=int(filters.get("customer_id") or 0) or None
    supplier_filter=int(filters.get("supplier_id") or 0) or None
    state_filter=(filters.get("state") or "").strip().upper()
    city_filter=(filters.get("city") or "").strip().lower()
    bank_filter=(filters.get("bank") or "").strip()
    wallet_filter=(filters.get("wallet") or "").strip()
    situation_filter=(filters.get("situation") or "").strip().lower()
    product_filter=(filters.get("product") or "").strip()
    seller_filter=(filters.get("seller") or "").strip()

    sales_company_ids = [x.id for x in sales_all]
    purchase_company_ids = [x.id for x in purchases_all]
    all_sales_items = db.scalars(select(SalesInvoiceItem).where(SalesInvoiceItem.sales_invoice_id.in_(sales_company_ids or [-1]))).all()
    all_purchase_items = db.scalars(select(PurchaseItem).where(PurchaseItem.purchase_invoice_id.in_(purchase_company_ids or [-1]))).all()
    matched_sales_ids = None
    matched_purchase_ids = None
    if product_filter:
        needle = product_filter.lower()
        matched_sales_ids = {i.sales_invoice_id for i in all_sales_items if needle in f"{i.product_code or ''} {i.description or ''}".lower()}
        matched_purchase_ids = {i.purchase_invoice_id for i in all_purchase_items if needle in f"{i.product_code or ''} {i.description or ''}".lower()}

    seller_options = sorted({s for x in sales_all if (s := invoice_seller(x)[0])} | {s for s in seller_by_title.values() if s})

    def partner_ok(pid: int|None, target: int|None=None) -> bool:
        if target and pid != target: return False
        p=partners.get(pid or -1)
        if state_filter and (str((p.state if p else "") or "").upper()!=state_filter): return False
        if city_filter and city_filter not in str((p.city if p else "") or "").lower(): return False
        return True

    def sale_ok(x: SalesInvoice) -> bool:
        if str(x.number or "").strip() in {"", "0"}: return False
        if not partner_ok(x.customer_id,customer_filter): return False
        payload=_raw_payload(raw_map,x.raw_record_id)
        if _is_cancelled(payload): return False
        if seller_filter and seller_filter.lower() not in str(invoice_seller(x)[0] or "").lower(): return False
        if matched_sales_ids is not None and x.id not in matched_sales_ids: return False
        return True

    sales_valid=[x for x in sales_all if sale_ok(x)]
    sales=[x for x in sales_valid if _in_period(x.issue_date,start,end)]
    sales_prev=[x for x in sales_valid if _in_period(x.issue_date,ps,pe)]
    sales_year=[x for x in sales_valid if _in_period(x.issue_date,ys,ye)]
    previous_month_start,previous_month_end=_shift_month(start,-1),_shift_month(end,-1)
    sales_previous_month=[x for x in sales_valid if _in_period(x.issue_date,previous_month_start,previous_month_end)]

    # Comparação configurável do Dashboard de Faturamento.
    # Padrão: mesmo intervalo deslocado em um mês (ex.: 01-21/out x 01-21/set).
    compare_mode=(filters.get("compare_mode") or "prev_month").strip().lower()
    compare_start, compare_end = previous_month_start, previous_month_end
    compare_label = "Mês anterior equivalente"
    if compare_mode == "previous_period":
        compare_start, compare_end = ps, pe
        compare_label = "Período anterior equivalente"
    elif compare_mode == "custom":
        try:
            c1=datetime.strptime((filters.get("compare_from") or ""), "%Y-%m-%d").date()
            c2=datetime.strptime((filters.get("compare_to") or ""), "%Y-%m-%d").date()
            compare_start, compare_end = min(c1,c2), max(c1,c2)
            compare_label = "Período comparativo personalizado"
        except Exception:
            compare_mode = "prev_month"
    sales_compare=[x for x in sales_valid if _in_period(x.issue_date,compare_start,compare_end)]
    sales_compare_total=sum(_money(x.total) for x in sales_compare)

    orders=[x for x in orders_all if _in_period(x.issue_date,start,end) and partner_ok(x.customer_id,customer_filter)]
    purchases=[x for x in purchases_all if str(x.number or "").strip() not in {"","0"} and _in_period(x.issue_date,start,end) and partner_ok(x.supplier_id,supplier_filter) and (matched_purchase_ids is None or x.id in matched_purchase_ids)]

    wallet_info_by_title = {
        t.id: _wallet_info(t, raw_map, receivable_detail_by_external, wallet_catalog)
        for t in titles_all if t.kind == "RECEBER"
    }
    bank_info_by_title = {
        t.id: _bank_info(t, raw_map, receivable_detail_by_external, bank_catalog)
        for t in titles_all
    }

    def title_bank_name(t: FinancialTitle) -> str:
        return str(bank_info_by_title.get(t.id, {}).get("name") or t.bank or "Sem banco").strip() or "Sem banco"

    def title_wallet_name(t: FinancialTitle) -> str:
        if t.kind == "RECEBER":
            return str(wallet_info_by_title.get(t.id, {}).get("name") or t.wallet or "Sem carteira").strip() or "Sem carteira"
        return str(t.wallet or "Sem carteira").strip() or "Sem carteira"

    def title_filter(t: FinancialTitle) -> bool:
        if bank_filter and title_bank_name(t) != bank_filter: return False
        if wallet_filter and title_wallet_name(t) != wallet_filter: return False
        resolved_pid = resolved_partner_by_title.get(t.id, t.partner_id) if t.kind == "RECEBER" else t.partner_id
        if customer_filter and t.kind=="RECEBER" and resolved_pid != customer_filter: return False
        if supplier_filter and t.kind in {"PAGAR","DESPESA"} and t.partner_id != supplier_filter: return False
        if seller_filter and t.kind == "RECEBER" and seller_filter.lower() not in str(seller_by_title.get(t.id) or "").lower(): return False
        if state_filter or city_filter:
            if not partner_ok(resolved_pid): return False
        if situation_filter:
            bal=max(_money(t.value)-_money(t.paid_value),0)
            due=_date(t.due_date)
            current = "pago" if bal<=0.005 else ("vencido" if due and due<today else ("parcial" if _money(t.paid_value)>0 else "aberto"))
            if situation_filter not in current: return False
        return True

    titles=[t for t in titles_all if title_filter(t)]
    ar_all=[t for t in titles if t.kind=="RECEBER"]
    ap_all=[t for t in titles if t.kind=="PAGAR"]
    exp_all=[t for t in titles if t.kind=="DESPESA"]
    ar_period=[t for t in ar_all if _in_period(t.issue_date,start,end)]
    ap_period=[t for t in ap_all if _in_period(t.issue_date,start,end)]
    exp_period=[t for t in exp_all if _in_period(t.issue_date,start,end)]

    date_coverage={
        "receber_issue": sum(1 for t in ar_all if _date(t.issue_date)),
        "pagar_issue": sum(1 for t in ap_all if _date(t.issue_date)),
        "despesa_issue": sum(1 for t in exp_all if _date(t.issue_date)),
        "receber_due": sum(1 for t in ar_all if _date(t.due_date)),
        "pagar_due": sum(1 for t in ap_all if _date(t.due_date)),
    }
    issued_receivables = None if ar_all and date_coverage["receber_issue"] == 0 else round(sum(_money(t.value) for t in ar_period),2)
    issued_payables = None if ap_all and date_coverage["pagar_issue"] == 0 else round(sum(_money(t.value) for t in ap_period),2)
    issued_expenses = None if exp_all and date_coverage["despesa_issue"] == 0 else round(sum(_money(t.value) for t in exp_period),2)

    dre_mappings = db.scalars(select(DreExpenseMapping).where(DreExpenseMapping.company_id == cid)).all()
    dre_mapping_by_category = {str(m.source_category or "").strip(): str(m.dre_group or "NAO_CLASSIFICADO") for m in dre_mappings}

    def expense_category(t: FinancialTitle) -> str:
        return _expense_category(_raw_payload(raw_map, t.raw_record_id))

    def expense_group(t: FinancialTitle) -> str:
        return dre_mapping_by_category.get(expense_category(t), "NAO_CLASSIFICADO")
    ar_open=[t for t in ar_all if max(_money(t.value)-_money(t.paid_value),0)>0.005]
    ap_open=[t for t in ap_all if max(_money(t.value)-_money(t.paid_value),0)>0.005]
    ar_overdue=[t for t in ar_open if _date(t.due_date) and _date(t.due_date)<today]
    ap_overdue=[t for t in ap_open if _date(t.due_date) and _date(t.due_date)<today]

    sales_total=sum(_money(x.total) for x in sales)
    sales_prev_total=sum(_money(x.total) for x in sales_prev)
    sales_year_total=sum(_money(x.total) for x in sales_year)
    purchases_total=0.0  # calculado abaixo com fallback seguro para NFStock/itens

    customer_sales=defaultdict(float)
    seller_sales=defaultdict(float)
    seller_sales_previous_month=defaultdict(float)
    seller_source_counts=defaultdict(int)
    channel_sales=defaultdict(float)
    state_sales=defaultdict(float)
    city_sales=defaultdict(float)
    for inv in sales:
        customer_sales[inv.customer_id]+= _money(inv.total)
        p=partners.get(inv.customer_id or -1)
        state_sales[str((p.state if p else None) or "Sem UF")]+= _money(inv.total)
        city_sales[str((p.city if p else None) or "Sem município")]+= _money(inv.total)
        payload=_raw_payload(raw_map,inv.raw_record_id)
        seller_name, seller_source = invoice_seller(inv)
        seller_source_counts[seller_source] += 1
        if seller_name:
            seller_sales[seller_name] += _money(inv.total)
        if _channel(payload):
            channel_sales[_channel(payload)] += _money(inv.total)
    for inv in sales_previous_month:
        seller_name, _ = invoice_seller(inv)
        if seller_name:
            seller_sales_previous_month[seller_name] += _money(inv.total)

    seller_rows=[]
    for seller_name in set(seller_sales) | set(seller_sales_previous_month):
        current=round(seller_sales.get(seller_name,0.0),2)
        previous=round(seller_sales_previous_month.get(seller_name,0.0),2)
        seller_rows.append({
            "name":seller_name,
            "value":current,
            "previous_month":previous,
            "growth":_variation(current,previous),
        })
    seller_rows.sort(key=lambda r:(r["value"],r["previous_month"]),reverse=True)

    sales_ids=[x.id for x in sales]
    sales_by_id={x.id:x for x in sales}
    items=[i for i in all_sales_items if i.sales_invoice_id in set(sales_ids)]
    if product_filter:
        needle=product_filter.lower()
        items=[i for i in items if needle in f"{i.product_code or ''} {i.description or ''}".lower()]
    prod_rev=defaultdict(float)
    prod_qty=defaultdict(float)
    product_meta_by_key={}
    cat_rev=defaultdict(float)
    subcat_rev=defaultdict(float)
    seller_category=defaultdict(float)
    customer_category=defaultdict(float)
    for i in items:
        meta=product_meta(i.product_code,i.description,getattr(i,"product_id",None))
        key=(meta["code"],meta["name"])
        product_meta_by_key[key]=meta
        prod_rev[key]+=_money(i.total)
        prod_qty[key]+=float(i.qty or 0)
        cat_rev[meta["category"]]+=_money(i.total)
        subcat_rev[meta["subcategory"]]+=_money(i.total)
        inv=sales_by_id.get(i.sales_invoice_id)
        if inv:
            seller_name, _ = invoice_seller(inv)
            if seller_name:
                seller_category[(seller_name,meta["category"])]+=_money(i.total)
            if inv.customer_id:
                customer_category[(inv.customer_id,meta["category"])]+=_money(i.total)

    product_revenue_rows=[]
    product_qty_rows=[]
    for key,value in sorted(prod_rev.items(),key=lambda kv:kv[1],reverse=True):
        meta=product_meta_by_key[key]
        product_revenue_rows.append({**meta,"value":round(value,2)})
    for key,value in sorted(prod_qty.items(),key=lambda kv:kv[1],reverse=True):
        meta=product_meta_by_key[key]
        product_qty_rows.append({**meta,"value":round(value,3)})
    seller_category_rows=[
        {"seller":seller,"category":category,"value":round(value,2)}
        for (seller,category),value in sorted(seller_category.items(),key=lambda kv:kv[1],reverse=True)
    ]
    seller_category_groups=[]
    for seller_row in seller_rows:
        seller_name=seller_row["name"]
        categories=[
            {"name":category,"value":round(value,2)}
            for (seller,category),value in seller_category.items() if seller==seller_name
        ]
        categories.sort(key=lambda r:r["value"],reverse=True)
        seller_category_groups.append({**seller_row,"categories":categories,"items_total":round(sum(r["value"] for r in categories),2)})

    purchase_ids=[x.id for x in purchases]
    pitems=[i for i in all_purchase_items if i.purchase_invoice_id in set(purchase_ids)]
    if product_filter:
        needle=product_filter.lower()
        pitems=[i for i in pitems if needle in f"{i.product_code or ''} {i.description or ''}".lower()]
    pitems_by_invoice=defaultdict(list)
    for i in pitems:
        pitems_by_invoice[i.purchase_invoice_id].append(i)

    # Algumas respostas de compras chegam com o total do cabeçalho zerado, embora
    # os itens tenham valores válidos. Para o BI, usa a melhor fonte disponível
    # sem duplicar: total ERPFlex -> total NFStock -> soma dos itens da própria NF.
    purchase_effective_values={}
    supplier_sums=defaultdict(float)
    for x in purchases:
        erp_total=_money(x.total_erpflex)
        nf_total=_money(x.total_nfstock)
        items_total=round(sum(_money(i.total) for i in pitems_by_invoice.get(x.id,[])),2)
        effective=erp_total if abs(erp_total)>0.005 else (nf_total if abs(nf_total)>0.005 else items_total)
        purchase_effective_values[x.id]=round(effective,2)
        supplier_sums[x.supplier_id]+=effective
    purchases_total=round(sum(purchase_effective_values.values()),2)

    purchase_prod=defaultdict(float)
    purchase_product_meta={}
    for i in pitems:
        meta=product_meta(i.product_code,i.description,None)
        key=(meta["code"],meta["name"])
        purchase_product_meta[key]=meta
        purchase_prod[key]+=_money(i.total)
    purchase_product_rows=[
        {**purchase_product_meta[key],"value":round(value,2)}
        for key,value in sorted(purchase_prod.items(),key=lambda kv:kv[1],reverse=True)
    ]

    # Clientes: datas de compra baseadas em NF válida para não confundir pedidos com faturamento.
    invoice_dates=defaultdict(list)
    for inv in sales_valid:
        d=_date(inv.issue_date)
        if inv.customer_id and d: invoice_dates[inv.customer_id].append(d)
    customer_ids={p.id for p in partners_list if p.role_customer}
    bought_period={pid for pid,dates in invoice_dates.items() if any(start<=d<=end for d in dates)}
    new_clients=0; recovered=0; inactive_30=inactive_60=inactive_90=0
    for pid in customer_ids:
        dates=sorted(invoice_dates.get(pid,[]))
        if dates and start<=dates[0]<=end: new_clients += 1
        if dates:
            gap=(today-dates[-1]).days
            inactive_30 += gap>30; inactive_60 += gap>60; inactive_90 += gap>90
            in_period=[d for d in dates if start<=d<=end]
            if in_period:
                first_period=min(in_period)
                prev=[d for d in dates if d<first_period]
                if prev and (first_period-max(prev)).days>60: recovered += 1
        else:
            inactive_30 += 1; inactive_60 += 1; inactive_90 += 1

    customer_rank=_rank(customer_sales,lambda pid:_partner_name(partners,pid),200)

    # ------------------------------------------------------------------
    # Dashboard de Faturamento V1.3.44 — estruturas de drill-down
    # ------------------------------------------------------------------
    def invoice_row(inv: SalesInvoice) -> dict:
        return {
            "id": inv.id,
            "number": str(inv.number or "—"),
            "date": inv.issue_date,
            "partner": _partner_name(partners, inv.customer_id),
            "value": _money(inv.total),
        }

    invoices_by_customer=defaultdict(list)
    seller_invoices=defaultdict(list)
    state_city_values=defaultdict(lambda: defaultdict(float))
    state_city_docs=defaultdict(lambda: defaultdict(int))
    state_city_invoices=defaultdict(lambda: defaultdict(list))
    for inv in sales:
        invoices_by_customer[inv.customer_id].append(invoice_row(inv))
        seller_name,_ = invoice_seller(inv)
        if seller_name:
            seller_invoices[seller_name].append(invoice_row(inv))
        partner=partners.get(inv.customer_id or -1)
        uf=str((partner.state if partner else None) or "Sem UF").strip() or "Sem UF"
        city=str((partner.city if partner else None) or "Sem município").strip() or "Sem município"
        state_city_values[uf][city]+=_money(inv.total)
        state_city_docs[uf][city]+=1
        state_city_invoices[uf][city].append(invoice_row(inv))

    # Produto -> NF-e, consolidando um produto uma única vez por nota.
    product_invoice_values=defaultdict(lambda: {"qty":0.0,"value":0.0})
    category_product_values=defaultdict(float)
    category_product_qty=defaultdict(float)
    for item in items:
        meta=product_meta(item.product_code,item.description,getattr(item,"product_id",None))
        pkey=(meta["code"],meta["name"],meta["category"],meta["subcategory"])
        cell=product_invoice_values[(pkey,item.sales_invoice_id)]
        cell["qty"]+=float(item.qty or 0)
        cell["value"]+=_money(item.total)
        category_product_values[(meta["category"],meta["subcategory"],pkey)]+=_money(item.total)
        category_product_qty[(meta["category"],meta["subcategory"],pkey)]+=float(item.qty or 0)

    product_dashboard=[]
    product_invoice_grouped=defaultdict(list)
    for (pkey,inv_id),cell in product_invoice_values.items():
        product_invoice_grouped[pkey].append((inv_id,cell))
    for pkey,entries in product_invoice_grouped.items():
        code,name,category,subcategory=pkey
        inv_rows=[]; qty=0.0; revenue=0.0
        for inv_id,cell in entries:
            inv=sales_by_id.get(inv_id)
            if not inv: continue
            qty+=cell["qty"]; revenue+=cell["value"]
            row=invoice_row(inv)
            row.update({"qty":round(cell["qty"],3),"item_value":round(cell["value"],2)})
            inv_rows.append(row)
        inv_rows.sort(key=lambda r: (_date(r["date"]) or date.min, r["number"]), reverse=True)
        product_dashboard.append({
            "code":code,"name":name,"category":category,"subcategory":subcategory,
            "qty":round(qty,3),"value":round(revenue,2),"invoice_count":len(inv_rows),"invoices":inv_rows,
        })
    product_dashboard.sort(key=lambda r:(r["qty"],r["value"]), reverse=True)
    product_dashboard_map={(r["code"],r["name"]):r for r in product_dashboard}

    category_dashboard=[]
    category_names=sorted({k[0] for k in category_product_values}, key=lambda cat: sum(v for (c,_,_),v in category_product_values.items() if c==cat), reverse=True)
    for category in category_names:
        sub_rows=[]
        sub_names=sorted({k[1] for k in category_product_values if k[0]==category}, key=lambda sub: sum(v for (c,s,_),v in category_product_values.items() if c==category and s==sub), reverse=True)
        category_value=0.0
        for sub in sub_names:
            products=[]; sub_value=0.0
            keys=[k for k in category_product_values if k[0]==category and k[1]==sub]
            keys.sort(key=lambda k:category_product_values[k], reverse=True)
            for _,_,pkey in keys:
                value=category_product_values[(category,sub,pkey)]
                qty=category_product_qty[(category,sub,pkey)]
                linked_product=product_dashboard_map.get((pkey[0],pkey[1]),{})
                products.append({"code":pkey[0],"name":pkey[1],"qty":round(qty,3),"value":round(value,2),"invoices":linked_product.get("invoices",[])})
                sub_value+=value
            category_value+=sub_value
            sub_rows.append({"name":sub,"value":round(sub_value,2),"products":products})
        category_dashboard.append({"name":category,"value":round(category_value,2),"subcategories":sub_rows})

    seller_dashboard=[]
    for row in seller_rows:
        invs=seller_invoices.get(row["name"],[])
        seller_dashboard.append({**row,"invoice_count":len(invs),"ticket":round(row["value"]/len(invs),2) if invs else 0.0,"invoices":invs})

    state_dashboard=[]
    for uf,city_map in state_city_values.items():
        cities=[{"name":city,"value":round(value,2),"invoice_count":state_city_docs[uf][city],"invoices":state_city_invoices[uf][city]} for city,value in city_map.items()]
        cities.sort(key=lambda r:r["value"],reverse=True)
        state_dashboard.append({"name":uf,"value":round(sum(city_map.values()),2),"invoice_count":sum(state_city_docs[uf].values()),"cities":cities})
    state_dashboard.sort(key=lambda r:r["value"],reverse=True)

    customer_dashboard=[]
    for row in customer_rank:
        invs=invoices_by_customer.get(row.get("key"),[])
        invs.sort(key=lambda r: (_date(r["date"]) or date.min, r["number"]), reverse=True)
        customer_dashboard.append({**row,"invoice_count":len(invs),"ticket":round(row["value"]/len(invs),2) if invs else 0.0,"invoices":invs})

    customer_category_groups=[]
    for customer_row in customer_rank:
        pid=customer_row.get("key")
        categories=[
            {"name":category,"value":round(value,2)}
            for (customer_id,category),value in customer_category.items() if customer_id==pid
        ]
        categories.sort(key=lambda r:r["value"],reverse=True)
        customer_category_groups.append({**customer_row,"categories":categories,"items_total":round(sum(r["value"] for r in categories),2)})
    customer_abc=_abc(customer_rank)
    total_customer_sales=sum(r["value"] for r in customer_rank)
    def concentration(n:int)->float:
        return round(sum(r["value"] for r in customer_rank[:n])/total_customer_sales*100,1) if total_customer_sales else 0.0

    # Financeiro realizado no período: data de baixa e valor baixado disponíveis no título.
    received=[t for t in ar_all if _in_period(t.paid_date,start,end) and _money(t.paid_value)>0]
    paid=[t for t in (ap_all+exp_all) if _in_period(t.paid_date,start,end) and _money(t.paid_value)>0]
    received_total=sum(_money(t.paid_value) for t in received)
    paid_total=sum(_money(t.paid_value) for t in paid)
    received_late=sum(_money(t.paid_value) for t in received if _date(t.due_date) and _date(t.paid_date) and _date(t.paid_date)>_date(t.due_date))
    received_on_time=sum(_money(t.paid_value) for t in received if _date(t.due_date) and _date(t.paid_date) and _date(t.paid_date)<=_date(t.due_date))

    wallet_names=sorted({title_wallet_name(t) for t in ar_all})
    wallet_results=[]
    for wallet_name in wallet_names:
        issued_value=None if issued_receivables is None else sum(_money(t.value) for t in ar_period if title_wallet_name(t)==wallet_name)
        received_value=sum(_money(t.paid_value) for t in received if title_wallet_name(t)==wallet_name)
        open_titles=[t for t in ar_open if title_wallet_name(t)==wallet_name]
        overdue_titles=[t for t in ar_overdue if title_wallet_name(t)==wallet_name]
        open_value=sum(max(_money(t.value)-_money(t.paid_value),0) for t in open_titles)
        overdue_value=sum(max(_money(t.value)-_money(t.paid_value),0) for t in overdue_titles)
        wallet_results.append({
            "name":wallet_name,
            "issued":round(issued_value,2) if issued_value is not None else None,
            "received":round(received_value,2),
            "open":round(open_value,2),
            "overdue":round(overdue_value,2),
            "open_count":len(open_titles),
            "overdue_pct":_pct(overdue_value,open_value) or 0.0,
        })
    wallet_results.sort(key=lambda r:(r["open"], r["issued"] if r["issued"] is not None else -1),reverse=True)

    ar_customer_balance=defaultdict(float); ar_customer_overdue=defaultdict(float)
    delay_days=defaultdict(list)
    ar_seller_open=defaultdict(float); ar_seller_overdue=defaultdict(float); ar_seller_received=defaultdict(float); ar_seller_issued=defaultdict(float)
    ar_seller_open_count=defaultdict(int); ar_seller_title_count=defaultdict(int); ar_seller_titles=defaultdict(list)
    for t in ar_all:
        customer_label = partner_label_by_title.get(t.id) or _partner_name(partners, t.partner_id)
        seller_label = seller_by_title.get(t.id) or "Sem vendedor identificado"
        bal=max(_money(t.value)-_money(t.paid_value),0)
        ar_seller_title_count[seller_label] += 1
        if t in ar_period:
            ar_seller_issued[seller_label] += _money(t.value)
        if t in received:
            ar_seller_received[seller_label] += _money(t.paid_value)
        if bal>0.005:
            ar_customer_balance[customer_label]+=bal
            ar_seller_open[seller_label]+=bal
            ar_seller_open_count[seller_label]+=1
            linked_inv=invoice_by_title.get(t.id)
            ar_seller_titles[seller_label].append({
                "id":t.id,"document":t.document or "—","partner":customer_label,"due":t.due_date,
                "balance":round(bal,2),"nf_number":(linked_inv.number if linked_inv else _specific_nf_from_payload(receivable_payload(t))),
                "invoice_id":(linked_inv.id if linked_inv else None),"bank":title_bank_name(t),"wallet":title_wallet_name(t),
            })
            if _date(t.due_date) and _date(t.due_date)<today:
                ar_customer_overdue[customer_label]+=bal
                ar_seller_overdue[seller_label]+=bal
        if _date(t.due_date) and _date(t.paid_date):
            delay_days[customer_label].append((_date(t.paid_date)-_date(t.due_date)).days)

    seller_receivable_rows=[]
    seller_keys=set(ar_seller_open) | set(ar_seller_received) | set(ar_seller_issued) | set(ar_seller_title_count)
    for seller_name in seller_keys:
        open_value=round(ar_seller_open.get(seller_name,0.0),2)
        overdue_value=round(ar_seller_overdue.get(seller_name,0.0),2)
        seller_receivable_rows.append({
            "name":seller_name,
            "issued":round(ar_seller_issued.get(seller_name,0.0),2) if issued_receivables is not None else None,
            "received":round(ar_seller_received.get(seller_name,0.0),2),
            "open":open_value,
            "overdue":overdue_value,
            "open_count":ar_seller_open_count.get(seller_name,0),
            "title_count":ar_seller_title_count.get(seller_name,0),
            "overdue_pct":_pct(overdue_value,open_value) if open_value else 0.0,
            "titles":sorted(ar_seller_titles.get(seller_name,[]), key=lambda r:_date(r.get("due")) or date.max),
        })
    seller_receivable_rows.sort(key=lambda r:(r["open"],r["overdue"],r["received"]), reverse=True)

    # Qualidade dos vínculos do financeiro. Em vez de transformar ausência de campo em zero,
    # a tela informa a cobertura efetiva dos dados sincronizados.
    ar_count=len(ar_all)
    partner_identified=sum(1 for t in ar_all if partner_label_by_title.get(t.id) not in {None,"","Não identificado","Sem cadastro"})
    bank_identified=sum(1 for t in ar_all if title_bank_name(t) and not title_bank_name(t).lower().startswith(("sem banco","banco ")))
    wallet_identified=sum(1 for t in ar_all if title_wallet_name(t) and not title_wallet_name(t).lower().startswith(("sem carteira","carteira ")))
    seller_identified=sum(1 for t in ar_all if seller_by_title.get(t.id))
    future_open=[t for t in ar_open if _date(t.due_date) and _date(t.due_date)>=today]
    receivable_quality={
        "total":ar_count,
        "partner":partner_identified, "partner_pct":round(partner_identified/ar_count*100,1) if ar_count else 100.0,
        "bank":bank_identified, "bank_pct":round(bank_identified/ar_count*100,1) if ar_count else 100.0,
        "wallet":wallet_identified, "wallet_pct":round(wallet_identified/ar_count*100,1) if ar_count else 100.0,
        "seller":seller_identified, "seller_pct":round(seller_identified/ar_count*100,1) if ar_count else 100.0,
        "issue":date_coverage["receber_issue"], "issue_pct":round(date_coverage["receber_issue"]/ar_count*100,1) if ar_count else 100.0,
        "due":date_coverage["receber_due"], "due_pct":round(date_coverage["receber_due"]/ar_count*100,1) if ar_count else 100.0,
    }

    # Fluxo futuro: não afirmar saldo bancário real; apenas saldo líquido projetado dos títulos conhecidos.
    inflows=_forecast(ar_open,today)
    outflows=_forecast(ap_open,today)
    if ar_open and not any(_date(t.due_date) for t in ar_open):
        inflows=[{**r,"value":None,"count":None} for r in inflows]
    if ap_open and not any(_date(t.due_date) for t in ap_open):
        outflows=[{**r,"value":None,"count":None} for r in outflows]
    cash_projection=[]
    for inc,out in zip(inflows,outflows):
        net=None if inc["value"] is None or out["value"] is None else round(inc["value"]-out["value"],2)
        cash_projection.append({"days":inc["days"],"in":inc["value"],"out":out["value"],"net":net})

    # Contas a pagar gerencial: previsto x realizado e pressão de caixa.
    # Previsto = valor nominal dos títulos PAGAR com vencimento no período selecionado.
    # Realizado = valor efetivamente baixado no caixa pela data de pagamento no mesmo período.
    # As duas bases são deliberadamente diferentes: pagamentos do período podem liquidar títulos antigos
    # ou antecipar títulos futuros. Por isso a diferença é tratada como desvio de caixa, não como saldo restante.
    ap_due_period=[t for t in ap_all if _in_period(t.due_date,start,end)]
    ap_paid_candidates=[t for t in ap_all if _money(t.paid_value)>0.005]
    ap_paid_period=[t for t in ap_all if _in_period(t.paid_date,start,end) and _money(t.paid_value)>0.005]
    payable_planned=None if ap_all and date_coverage["pagar_due"]==0 else round(sum(_money(t.value) for t in ap_due_period),2)
    payable_realized=None if ap_paid_candidates and not any(_date(t.paid_date) for t in ap_paid_candidates) else round(sum(_money(t.paid_value) for t in ap_paid_period),2)
    payable_variance=None if payable_planned is None or payable_realized is None else round(payable_realized-payable_planned,2)
    payable_execution_pct=None if payable_planned is None or payable_realized is None else _pct(payable_realized,payable_planned)

    ap_paid_late=[t for t in ap_paid_period if _date(t.due_date) and _date(t.paid_date) and _date(t.paid_date)>_date(t.due_date)]
    payable_paid_late=round(sum(_money(t.paid_value) for t in ap_paid_late),2)
    payable_paid_late_count=len(ap_paid_late)

    payable_aging=_aging(ap_open,today)
    overdue_aging=[row for row in _aging(ap_overdue,today) if row["name"]!="A vencer"]
    ap_overdue_supplier=defaultdict(float)
    for t in ap_overdue:
        ap_overdue_supplier[t.partner_id]+=max(_money(t.value)-_money(t.paid_value),0)

    # Curto prazo combina obrigações a pagar com recebíveis já conhecidos, sem alegar saldo bancário real.
    short_cashflow=[]
    for inc,out in zip(inflows,outflows):
        if inc["days"] not in (7,15,30):
            continue
        net=None if inc["value"] is None or out["value"] is None else round(inc["value"]-out["value"],2)
        coverage=None if inc["value"] is None or out["value"] in (None,0) else _pct(inc["value"],out["value"])
        short_cashflow.append({
            "days":inc["days"],"payables":out["value"],"payables_count":out["count"],
            "receivables":inc["value"],"receivables_count":inc["count"],"net":net,"coverage_pct":coverage,
        })

    payable_due_coverage_pct=round(date_coverage["pagar_due"]/len(ap_all)*100,1) if ap_all else 100.0
    payable_paid_date_coverage_pct=round(sum(1 for t in ap_paid_candidates if _date(t.paid_date))/len(ap_paid_candidates)*100,1) if ap_paid_candidates else 100.0

    # DRE Gerencial: usa somente receita fiscal válida e lançamentos DESPESA.
    # PAGAR não é somado às despesas para evitar duplicidade financeira/competência.
    exp_prev=[t for t in exp_all if _in_period(t.issue_date,ps,pe)]
    dre_group_sums=defaultdict(float)
    dre_group_prev=defaultdict(float)
    dre_category_sums=defaultdict(float)
    dre_category_counts=defaultdict(int)
    for t in exp_all:
        category=expense_category(t)
        if t in exp_period:
            dre_category_sums[category]+=_money(t.value)
            dre_category_counts[category]+=1
            dre_group_sums[expense_group(t)]+=_money(t.value)
        if t in exp_prev:
            dre_group_prev[expense_group(t)]+=_money(t.value)

    dre_expenses_total_calc=round(sum(_money(t.value) for t in exp_period),2)
    dre_expenses_prev_calc=round(sum(_money(t.value) for t in exp_prev),2)
    expense_period_available = not exp_all or date_coverage["despesa_issue"] > 0
    dre_expenses_total=dre_expenses_total_calc if expense_period_available else None
    dre_expenses_prev=dre_expenses_prev_calc if expense_period_available else None
    dre_partial_result=round(sales_total-dre_expenses_total_calc,2) if expense_period_available else None
    dre_partial_prev=round(sales_prev_total-dre_expenses_prev_calc,2) if expense_period_available else None
    dre_unclassified=round(dre_group_sums.get("NAO_CLASSIFICADO",0.0),2) if expense_period_available else None

    dre_group_rows=[]
    for code in ("COMERCIAL","ADMINISTRATIVA","LOGISTICA","OUTRAS_OPERACIONAIS","FINANCEIRA","NAO_CLASSIFICADO"):
        current=round(dre_group_sums.get(code,0.0),2)
        previous=round(dre_group_prev.get(code,0.0),2)
        dre_group_rows.append({
            "code":code,"name":_dre_group_label(code),
            "value":current if expense_period_available else None,
            "previous":previous if expense_period_available else None,
            "variation":_variation(current,previous) if expense_period_available else None,
            "share_revenue":_pct(current,sales_total) if expense_period_available else None,
        })

    dre_rows=[
        {"kind":"revenue","label":"Receita Bruta de Vendas","value":round(sales_total,2),"previous":round(sales_prev_total,2),"variation":_variation(sales_total,sales_prev_total),"available":True,"note":"NF-e de saída válidas, excluindo número fiscal zero e documentos cancelados conforme regra do BI."},
        {"kind":"unavailable","label":"(-) Deduções, devoluções e tributos sobre vendas","value":None,"previous":None,"variation":None,"available":False,"note":"Dados ainda não normalizados de forma suficientemente confiável para compor a DRE."},
        {"kind":"unavailable total","label":"= Receita Líquida","value":None,"previous":None,"variation":None,"available":False,"note":"Depende das deduções/tributos acima."},
        {"kind":"unavailable","label":"(-) CMV / Custo dos Produtos Vendidos","value":None,"previous":None,"variation":None,"available":False,"note":"Compras do mês não são usadas como CMV. É necessário custo efetivo/médio associado às vendas."},
        {"kind":"unavailable total","label":"= Lucro Bruto","value":None,"previous":None,"variation":None,"available":False,"note":"Depende da Receita Líquida e do CMV."},
    ]
    for row in dre_group_rows:
        dre_rows.append({"kind":"expense" if row["code"]!="NAO_CLASSIFICADO" else "expense warning","label":f"(-) {row['name']}","value":row["value"],"previous":row["previous"],"variation":row["variation"],"available":expense_period_available,"note":"Classificação configurável a partir da categoria real retornada na despesa." if expense_period_available else "Existem despesas, mas a data de lançamento/emissão não foi identificada nos registros sincronizados."})
    dre_rows += [
        {"kind":"total","label":"Total de despesas conhecidas","value":dre_expenses_total,"previous":dre_expenses_prev,"variation":_variation(dre_expenses_total_calc,dre_expenses_prev_calc) if expense_period_available else None,"available":expense_period_available,"note":"Somente lançamentos do módulo Despesas; Contas a Pagar não é somado novamente." if expense_period_available else "Despesas existentes sem data de lançamento/emissão reconhecida."},
        {"kind":"partial total","label":"= Resultado gerencial parcial","value":dre_partial_result,"previous":dre_partial_prev,"variation":_variation(dre_partial_result,dre_partial_prev) if expense_period_available else None,"available":expense_period_available,"note":"Receita bruta menos despesas conhecidas. Não equivale ao lucro contábil enquanto deduções, tributos e CMV estiverem indisponíveis." if expense_period_available else "Resultado indisponível enquanto as datas das despesas não forem identificadas."},
    ]

    discovered_categories=sorted(set(expense_category(t) for t in exp_all), key=lambda x:x.lower())
    dre_category_rows=[]
    for category in discovered_categories:
        code=dre_mapping_by_category.get(category,"NAO_CLASSIFICADO")
        dre_category_rows.append({
            "category":category,"group":code,"group_name":_dre_group_label(code),
            "value":round(dre_category_sums.get(category,0.0),2),"count":dre_category_counts.get(category,0),
        })

    selected_year=end.year
    dre_monthly=[]
    month_sales=defaultdict(float)
    month_expenses=defaultdict(lambda: defaultdict(float))
    for inv in sales_valid:
        d=_date(inv.issue_date)
        if d and d.year==selected_year:
            month_sales[d.month]+=_money(inv.total)
    for t in exp_all:
        d=_date(t.issue_date)
        if d and d.year==selected_year:
            month_expenses[d.month][expense_group(t)]+=_money(t.value)
    for month in range(1,13):
        sales_m=round(month_sales.get(month,0.0),2)
        groups_calc={code:round(month_expenses[month].get(code,0.0),2) for code in DRE_GROUPS}
        expenses_m_calc=round(sum(groups_calc.values()),2)
        groups=groups_calc if expense_period_available else {code:None for code in DRE_GROUPS}
        dre_monthly.append({
            "month":month,"label":calendar.month_abbr[month].replace("Jan","Jan").replace("Feb","Fev").replace("Apr","Abr").replace("May","Mai").replace("Aug","Ago").replace("Sep","Set").replace("Oct","Out").replace("Dec","Dez"),
            "sales":sales_m,"expenses":expenses_m_calc if expense_period_available else None,"partial":round(sales_m-expenses_m_calc,2) if expense_period_available else None,"groups":groups,
        })

    dre_expense_details=[]
    for t in sorted(exp_period,key=lambda x:_date(x.issue_date) or date.min,reverse=True)[:250]:
        category=expense_category(t); code=expense_group(t)
        dre_expense_details.append({
            "id":t.id,"date":t.issue_date,"document":t.document or "—","partner":_partner_name(partners,t.partner_id),
            "category":category,"group":_dre_group_label(code),"group_code":code,"value":_money(t.value),"status":t.status or "—",
        })

    # Logística vinculada apenas a NFs válidas do período.
    sale_ids_set=set(sales_ids)
    deliveries=[d for d in deliveries_all if d.sales_invoice_id in sale_ids_set]
    logistics_status=defaultdict(int); logistics_region=defaultdict(int)
    for d in deliveries:
        logistics_status[str(d.status or "Sem status")]+=1; logistics_region[str(d.region or "Sem região")]+=1
    freight=sum(_money(d.freight_cost) for d in deliveries)

    # Alertas apenas com regras verificáveis a partir da base atual.
    ar_open_total=sum(max(_money(t.value)-_money(t.paid_value),0) for t in ar_open)
    ar_overdue_total=sum(max(_money(t.value)-_money(t.paid_value),0) for t in ar_overdue)
    overdue_pct=_pct(ar_overdue_total,ar_open_total) or 0.0
    alerts=[]
    if overdue_pct>=10:
        alerts.append({"level":"danger" if overdue_pct>=25 else "warning","title":"Inadimplência relevante","detail":f"{overdue_pct:.1f}% da carteira em aberto está vencida.","value":ar_overdue_total,"action":"Revisar aging e clientes com maior saldo vencido."})
    if concentration(5)>=50:
        alerts.append({"level":"warning","title":"Concentração de faturamento","detail":f"Os 5 maiores clientes representam {concentration(5):.1f}% do faturamento do período.","value":None,"action":"Acompanhar risco de dependência dos maiores clientes."})
    if inactive_60:
        alerts.append({"level":"info","title":"Clientes sem compras recentes","detail":f"{inactive_60} clientes cadastrados não possuem compra fiscal nos últimos 60 dias.","value":None,"action":"Priorizar carteira para recuperação comercial."})
    negative=[x for x in cash_projection if x["net"] is not None and x["net"]<0]
    if negative:
        first=negative[0]
        alerts.append({"level":"warning","title":"Fluxo projetado negativo","detail":f"Nos próximos {first['days']} dias, pagamentos previstos superam recebimentos em R$ {abs(first['net']):,.2f}.","value":first["net"],"action":"Revisar vencimentos e disponibilidade de caixa; isto não é saldo bancário real."})

    unavailable=[]
    if not seller_sales:
        unavailable.append("Indicadores por vendedor: nenhum vendedor foi localizado no faturamento nem nos pedidos vinculados do período. Sincronize Pedidos e Faturamento com enriquecimento para validar os campos reais retornados pela API.")
    unavailable += [
        "Margem e lucro bruto: custo efetivo/CPV por venda não está normalizado de forma confiável.",
        "Estoque, lote e validade: não existem saldo, lote e data de validade no cadastro de produtos atual.",
        "Saldo bancário real: não existe integração de extrato/saldo bancário; o fluxo de caixa mostra apenas títulos conhecidos.",
        "Entregas no prazo: não há timestamps normalizados suficientes de promessa e conclusão da entrega.",
    ]


    # ------------------------------------------------------------------
    # V1.3.47 — Drill-down transversal do BI
    # ------------------------------------------------------------------
    # Todos os agrupamentos abaixo apontam para documentos/títulos reais já
    # normalizados. Nenhum detalhe é reconstruído por aproximação textual.
    def ar_detail_row(t: FinancialTitle) -> dict:
        linked_inv=invoice_by_title.get(t.id)
        return {
            "id":t.id,"document":t.document or "—",
            "partner":partner_label_by_title.get(t.id) or _partner_name(partners,t.partner_id),
            "seller":seller_by_title.get(t.id) or "Sem vendedor identificado",
            "nf_number":(linked_inv.number if linked_inv else _specific_nf_from_payload(receivable_payload(t))),
            "invoice_id":(linked_inv.id if linked_inv else None),
            "issue":t.issue_date,"due":t.due_date,"paid_date":t.paid_date,
            "value":_money(t.value),"paid":_money(t.paid_value),
            "balance":round(max(_money(t.value)-_money(t.paid_value),0),2),
            "bank":title_bank_name(t),"wallet":title_wallet_name(t),
        }

    def ap_detail_row(t: FinancialTitle) -> dict:
        due=_date(t.due_date)
        paid_date=_date(t.paid_date)
        bal=round(max(_money(t.value)-_money(t.paid_value),0),2)
        return {
            "id":t.id,"document":t.document or "—","partner":_partner_name(partners,t.partner_id),
            "issue":t.issue_date,"due":t.due_date,"paid_date":t.paid_date,
            "value":_money(t.value),"paid":_money(t.paid_value),"balance":bal,
            "bank":title_bank_name(t),"wallet":title_wallet_name(t),
            "status":("Pago" if bal<=0.005 else ("Vencido" if due and due<today else ("A vencer" if due else "Sem vencimento"))),
            "days_overdue":((today-due).days if due and due<today and bal>0.005 else 0),
            "paid_late":bool(due and paid_date and paid_date>due and _money(t.paid_value)>0.005),
        }

    def aging_bucket(t: FinancialTitle) -> str:
        due=_date(t.due_date)
        if not due or due>=today:
            return "A vencer"
        days=(today-due).days
        if days<=7: return "1 a 7 dias"
        if days<=15: return "8 a 15 dias"
        if days<=30: return "16 a 30 dias"
        if days<=60: return "31 a 60 dias"
        if days<=90: return "61 a 90 dias"
        return "Acima de 90 dias"

    receivable_aging_details=[]
    for row in _aging(ar_open,today):
        matched=[ar_detail_row(t) for t in ar_open if aging_bucket(t)==row["name"]]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        receivable_aging_details.append({**row,"titles":matched[:250]})

    receivable_forecast_details=[]
    for row in inflows:
        limit=today+timedelta(days=row["days"])
        matched=[ar_detail_row(t) for t in ar_open if _date(t.due_date) and today<=_date(t.due_date)<=limit]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        receivable_forecast_details.append({**row,"titles":matched[:250]})

    bank_drill=[]
    for bank_name in sorted({title_bank_name(t) for t in ar_open}):
        matched=[ar_detail_row(t) for t in ar_open if title_bank_name(t)==bank_name]
        value=round(sum(r["balance"] for r in matched),2)
        bank_drill.append({"name":bank_name,"value":value,"count":len(matched),"titles":matched[:250]})
    bank_drill.sort(key=lambda r:r["value"],reverse=True)

    for row in wallet_results:
        matched=[ar_detail_row(t) for t in ar_open if title_wallet_name(t)==row["name"]]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        row["titles"]=matched[:250]

    customer_open_drill=[]
    for name,value in sorted(ar_customer_balance.items(),key=lambda kv:kv[1],reverse=True):
        matched=[ar_detail_row(t) for t in ar_open if (partner_label_by_title.get(t.id) or _partner_name(partners,t.partner_id))==name]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        customer_open_drill.append({"name":name,"value":round(value,2),"titles":matched[:250]})
    customer_overdue_drill=[]
    for name,value in sorted(ar_customer_overdue.items(),key=lambda kv:kv[1],reverse=True):
        matched=[ar_detail_row(t) for t in ar_overdue if (partner_label_by_title.get(t.id) or _partner_name(partners,t.partner_id))==name]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        customer_overdue_drill.append({"name":name,"value":round(value,2),"titles":matched[:250]})

    payable_aging_details=[]
    for row in payable_aging:
        matched=[ap_detail_row(t) for t in ap_open if aging_bucket(t)==row["name"]]
        matched.sort(key=lambda r:_date(r.get("due")) or date.max)
        payable_aging_details.append({**row,"titles":matched[:250]})

    payable_supplier_drill=[]
    for pid in {t.partner_id for t in ap_open}:
        matched=[ap_detail_row(t) for t in ap_open if t.partner_id==pid]
        value=round(sum(r["balance"] for r in matched),2)
        payable_supplier_drill.append({"key":pid,"name":_partner_name(partners,pid),"value":value,"titles":matched[:250]})
    payable_supplier_drill.sort(key=lambda r:r["value"],reverse=True)
    payable_overdue_supplier_drill=[]
    for pid in {t.partner_id for t in ap_overdue}:
        matched=[ap_detail_row(t) for t in ap_overdue if t.partner_id==pid]
        value=round(sum(r["balance"] for r in matched),2)
        payable_overdue_supplier_drill.append({"key":pid,"name":_partner_name(partners,pid),"value":value,"titles":matched[:250]})
    payable_overdue_supplier_drill.sort(key=lambda r:r["value"],reverse=True)

    payable_planned_titles=[ap_detail_row(t) for t in sorted(ap_due_period,key=lambda x:_date(x.due_date) or date.max)]
    payable_realized_titles=[ap_detail_row(t) for t in sorted(ap_paid_period,key=lambda x:_date(x.paid_date) or date.max,reverse=True)]
    payable_paid_late_titles=[ap_detail_row(t) for t in sorted(ap_paid_late,key=lambda x:_date(x.paid_date) or date.max,reverse=True)]

    for row in short_cashflow:
        limit=today+timedelta(days=row["days"])
        row["payable_titles"]=[ap_detail_row(t) for t in ap_open if _date(t.due_date) and today<=_date(t.due_date)<=limit][:250]
        row["receivable_titles"]=[ar_detail_row(t) for t in ar_open if _date(t.due_date) and today<=_date(t.due_date)<=limit][:250]

    cash_projection_drill=[]
    for row in cash_projection:
        limit=today+timedelta(days=row["days"])
        cash_projection_drill.append({**row,
            "receivable_titles":[ar_detail_row(t) for t in ar_open if _date(t.due_date) and today<=_date(t.due_date)<=limit][:250],
            "payable_titles":[ap_detail_row(t) for t in ap_open if _date(t.due_date) and today<=_date(t.due_date)<=limit][:250],
        })

    cash_received_details=[ar_detail_row(t) for t in sorted(received,key=lambda x:_date(x.paid_date) or date.min,reverse=True)[:250]]
    cash_paid_details=[ap_detail_row(t) for t in sorted(paid,key=lambda x:_date(x.paid_date) or date.min,reverse=True)[:250]]

    # DRE: cada grupo/categoria recebe os lançamentos que formam o valor.
    dre_details_by_group=defaultdict(list)
    dre_details_by_category=defaultdict(list)
    for t in exp_period:
        detail={
            "id":t.id,"date":t.issue_date,"document":t.document or "—","partner":_partner_name(partners,t.partner_id),
            "category":expense_category(t),"group":_dre_group_label(expense_group(t)),"value":_money(t.value),"status":t.status or "—",
        }
        dre_details_by_group[expense_group(t)].append(detail)
        dre_details_by_category[expense_category(t)].append(detail)
    for row in dre_group_rows:
        row["details"]=sorted(dre_details_by_group.get(row["code"],[]),key=lambda r:_date(r.get("date")) or date.min,reverse=True)[:250]
    for row in dre_category_rows:
        row["details"]=sorted(dre_details_by_category.get(row["category"],[]),key=lambda r:_date(r.get("date")) or date.min,reverse=True)[:250]

    # Comercial: vendedor -> categoria -> produto -> NF-e.
    seller_cat_product=defaultdict(lambda: defaultdict(lambda: defaultdict(lambda:{"qty":0.0,"value":0.0,"invoice_ids":set()})))
    customer_cat_product=defaultdict(lambda: defaultdict(lambda: defaultdict(lambda:{"qty":0.0,"value":0.0,"invoice_ids":set()})))
    for item in items:
        inv=sales_by_id.get(item.sales_invoice_id)
        if not inv:
            continue
        meta=product_meta(item.product_code,item.description,getattr(item,"product_id",None))
        pkey=(meta["code"],meta["name"])
        seller_name,_=invoice_seller(inv)
        if seller_name:
            cell=seller_cat_product[seller_name][meta["category"]][pkey]
            cell["qty"]+=float(item.qty or 0); cell["value"]+=_money(item.total); cell["invoice_ids"].add(inv.id)
        if inv.customer_id:
            cell=customer_cat_product[inv.customer_id][meta["category"]][pkey]
            cell["qty"]+=float(item.qty or 0); cell["value"]+=_money(item.total); cell["invoice_ids"].add(inv.id)

    def invoice_rows_from_ids(ids:set[int]) -> list[dict]:
        rows=[invoice_row(sales_by_id[i]) for i in ids if i in sales_by_id]
        rows.sort(key=lambda r:_date(r.get("date")) or date.min,reverse=True)
        return rows

    for group in seller_category_groups:
        cats=[]
        for cat in group.get("categories",[]):
            prods=[]
            for (code,name),cell in seller_cat_product.get(group["name"],{}).get(cat["name"],{}).items():
                prods.append({"code":code,"name":name,"qty":round(cell["qty"],3),"value":round(cell["value"],2),"invoices":invoice_rows_from_ids(cell["invoice_ids"])})
            prods.sort(key=lambda r:r["value"],reverse=True)
            cats.append({**cat,"products":prods})
        group["categories"]=cats
        group["invoices"]=seller_invoices.get(group["name"],[])[:250]

    for group in customer_category_groups:
        pid=group.get("key")
        cats=[]
        for cat in group.get("categories",[]):
            prods=[]
            for (code,name),cell in customer_cat_product.get(pid,{}).get(cat["name"],{}).items():
                prods.append({"code":code,"name":name,"qty":round(cell["qty"],3),"value":round(cell["value"],2),"invoices":invoice_rows_from_ids(cell["invoice_ids"])})
            prods.sort(key=lambda r:r["value"],reverse=True)
            cats.append({**cat,"products":prods})
        group["categories"]=cats
        group["invoices"]=invoices_by_customer.get(pid,[])[:250]

    # Compras: fornecedor -> NF-e -> itens e produto -> NF-e/fornecedor.
    purchase_invoice_rows={}
    for inv in purchases:
        item_rows=[]
        for item in pitems_by_invoice.get(inv.id,[]):
            meta=product_meta(item.product_code,item.description,None)
            item_rows.append({"code":meta["code"],"name":meta["name"],"qty":round(float(item.qty or 0),3),"unit_price":_money(item.unit_price),"value":_money(item.total)})
        purchase_invoice_rows[inv.id]={
            "id":inv.id,"number":inv.number or "—","date":inv.issue_date,"partner":_partner_name(partners,inv.supplier_id),
            "value":purchase_effective_values.get(inv.id,0.0),"reconciliation":inv.reconciliation_status or "—","items":item_rows,
        }
    purchase_supplier_drill=[]
    for pid in {x.supplier_id for x in purchases}:
        docs=[purchase_invoice_rows[x.id] for x in purchases if x.supplier_id==pid]
        docs.sort(key=lambda r:_date(r.get("date")) or date.min,reverse=True)
        purchase_supplier_drill.append({"key":pid,"name":_partner_name(partners,pid),"value":round(sum(d["value"] for d in docs),2),"documents":docs})
    purchase_supplier_drill.sort(key=lambda r:r["value"],reverse=True)

    purchase_product_drill=[]
    pprod=defaultdict(lambda:{"meta":None,"qty":0.0,"value":0.0,"invoice_ids":set()})
    for item in pitems:
        meta=product_meta(item.product_code,item.description,None)
        key=(meta["code"],meta["name"])
        cell=pprod[key]; cell["meta"]=meta; cell["qty"]+=float(item.qty or 0); cell["value"]+=_money(item.total); cell["invoice_ids"].add(item.purchase_invoice_id)
    for key,cell in pprod.items():
        docs=[purchase_invoice_rows[i] for i in cell["invoice_ids"] if i in purchase_invoice_rows]
        docs.sort(key=lambda r:_date(r.get("date")) or date.min,reverse=True)
        purchase_product_drill.append({**cell["meta"],"qty":round(cell["qty"],3),"value":round(cell["value"],2),"documents":docs})
    purchase_product_drill.sort(key=lambda r:r["value"],reverse=True)

    # Logística: status/região -> entregas -> NF-e/cliente.
    delivery_rows=[]
    for d in deliveries:
        inv=sales_by_id.get(d.sales_invoice_id)
        delivery_rows.append({
            "id":d.id,"status":d.status or "Sem status","region":d.region or "Sem região","responsible":d.responsible or "—",
            "volumes":round(float(d.volumes or 0),2),"freight":_money(d.freight_cost),
            "invoice_id":inv.id if inv else None,"nf":inv.number if inv else "—",
            "customer":_partner_name(partners,inv.customer_id) if inv else "Sem cadastro",
        })
    logistics_status_drill=[]
    for name in sorted({r["status"] for r in delivery_rows}):
        rows=[r for r in delivery_rows if r["status"]==name]
        logistics_status_drill.append({"name":name,"value":len(rows),"deliveries":rows})
    logistics_status_drill.sort(key=lambda r:r["value"],reverse=True)
    logistics_region_drill=[]
    for name in sorted({r["region"] for r in delivery_rows}):
        rows=[r for r in delivery_rows if r["region"]==name]
        logistics_region_drill.append({"name":name,"value":len(rows),"deliveries":rows})
    logistics_region_drill.sort(key=lambda r:r["value"],reverse=True)

    # Alertas levam o usuário diretamente ao BI que contém o detalhe operacional.
    alert_target_by_title={
        "Inadimplência relevante":"/bi/contas-receber",
        "Concentração de faturamento":"/bi/clientes",
        "Clientes sem compras recentes":"/bi/clientes",
        "Fluxo projetado negativo":"/bi/fluxo-caixa",
    }
    for alert in alerts:
        alert["target_url"]=alert_target_by_title.get(alert.get("title"),"/bi/visao-geral")

    latest_sync=_latest_sync(db,cid)
    options={
        "customers":[{"id":p.id,"name":_partner_name(partners,p.id)} for p in partners_list if p.role_customer],
        "suppliers":[{"id":p.id,"name":_partner_name(partners,p.id)} for p in partners_list if p.role_supplier],
        "states":sorted({str(p.state or "").strip() for p in partners_list if str(p.state or "").strip()}),
        "cities":sorted({str(p.city or "").strip() for p in partners_list if str(p.city or "").strip()}),
        "banks":sorted({title_bank_name(t) for t in titles_all if title_bank_name(t)}),
        "wallets":sorted({title_wallet_name(t) for t in titles_all if t.kind == "RECEBER" and title_wallet_name(t)}),
        "sellers":seller_options,
        "products":sorted({str(p.code or p.description or "").strip() for p in products_list if str(p.code or p.description or "").strip()})[:1000],
    }

    result={
        "meta":{"start":start,"end":end,"previous_start":ps,"previous_end":pe,"year_start":ys,"year_end":ye,"period":period,"latest_sync":latest_sync,"filters":filters,"options":options,"data_quality":{"date_coverage":date_coverage,"receivables_total":len(ar_all),"payables_total":len(ap_all),"expenses_total":len(exp_all)}},
        "overview":{
            "sales":round(sales_total,2),"sales_prev":round(sales_prev_total,2),"sales_var":_variation(sales_total,sales_prev_total),
            "sales_year_ago":round(sales_year_total,2),"sales_yoy":_variation(sales_total,sales_year_total),"sales_docs":len(sales),"sales_ticket":round(sales_total/len(sales),2) if sales else 0,
            "ar_open":round(ar_open_total,2),"ap_open":round(sum(max(_money(t.value)-_money(t.paid_value),0) for t in ap_open),2),"received":round(received_total,2),"paid":round(paid_total,2),
            "overdue":round(ar_overdue_total,2),"overdue_pct":overdue_pct,"customers":len(customer_ids),"customers_bought":len(bought_period),"new_customers":new_clients,"inactive_60":inactive_60,
            "operating_expenses":issued_expenses,"projected_30_net":next((x["net"] for x in cash_projection if x["days"]==30),None),
        },
        "sales":{
            "total":round(sales_total,2),"previous":round(sales_prev_total,2),"variation":_variation(sales_total,sales_prev_total),"year_ago":round(sales_year_total,2),"yoy":_variation(sales_total,sales_year_total),
            "docs":len(sales),"ticket":round(sales_total/len(sales),2) if sales else 0,"qty":round(sum(prod_qty.values()),3),"avg_unit_price":round(sum(prod_rev.values())/sum(prod_qty.values()),2) if sum(prod_qty.values()) else 0,
            "customers":customer_rank[:20],"products_revenue":product_revenue_rows[:20],"products_qty":product_qty_rows[:20],
            "categories":_rank(cat_rev,limit=20),"subcategories":_rank(subcat_rev,limit=20),"states":_rank(state_sales,limit=20),"cities":_rank(city_sales,limit=20),"channels":_rank(channel_sales,limit=20),"sellers":seller_rows[:20],
            "documents":[{"id":x.id,"number":x.number,"date":x.issue_date,"partner":_partner_name(partners,x.customer_id),"value":_money(x.total)} for x in sorted(sales,key=lambda x:_date(x.issue_date) or date.min,reverse=True)[:100]],
        },
        "sales_dashboard":{
            "period_label": "Faturamento do dia" if start==end==today else "Faturamento do período",
            "current": round(sales_total,2),
            "compare": round(sales_compare_total,2),
            "variation": _variation(sales_total,sales_compare_total),
            "compare_mode": compare_mode,
            "compare_label": compare_label,
            "compare_start": compare_start,
            "compare_end": compare_end,
            "products": product_dashboard[:100],
            "categories": category_dashboard[:100],
            "sellers": seller_dashboard[:100],
            "states": state_dashboard[:100],
            "customers": customer_dashboard[:200],
            "documents": [invoice_row(x) for x in sorted(sales,key=lambda x:_date(x.issue_date) or date.min,reverse=True)],
        },
        "receivables":{
            "open":round(ar_open_total,2),"overdue":round(ar_overdue_total,2),"due":round(ar_open_total-ar_overdue_total,2),"issued":issued_receivables,"received":round(received_total,2),"received_late":round(received_late,2),
            "on_time_pct":_pct(received_on_time,received_total),"overdue_pct":overdue_pct,"aging":receivable_aging_details,"forecast":receivable_forecast_details,
            "future_open_count":len(future_open),"future_open_value":round(sum(max(_money(t.value)-_money(t.paid_value),0) for t in future_open),2),
            "top_balance":customer_open_drill[:20],"top_overdue":customer_overdue_drill[:20],
            "top_delay":[{"name":name,"value":round(sum(ds)/len(ds),1)} for name,ds in sorted(delay_days.items(),key=lambda kv:(sum(kv[1])/len(kv[1])) if kv[1] else 0,reverse=True)[:20] if ds],
            "banks":bank_drill[:20],
            "wallets":_rank(defaultdict(float,{k:sum(max(_money(t.value)-_money(t.paid_value),0) for t in ar_open if title_wallet_name(t)==k) for k in {title_wallet_name(t) for t in ar_open}}),limit=20),
            "wallet_results":wallet_results,"sellers":seller_receivable_rows[:100],"quality":receivable_quality,
            "titles":[{
                "id":t.id,"document":t.document,"partner":partner_label_by_title.get(t.id) or _partner_name(partners,t.partner_id),
                "partner_source":partner_source_by_title.get(t.id,""),"seller":seller_by_title.get(t.id) or "Sem vendedor identificado",
                "seller_source":seller_source_by_title.get(t.id,""),"nf_number":(invoice_by_title.get(t.id).number if invoice_by_title.get(t.id) else _specific_nf_from_payload(receivable_payload(t))),
                "invoice_id":(invoice_by_title.get(t.id).id if invoice_by_title.get(t.id) else None),
                "due":t.due_date,"issue":t.issue_date,"value":_money(t.value),"paid":_money(t.paid_value),
                "balance":round(max(_money(t.value)-_money(t.paid_value),0),2),"bank":title_bank_name(t),"wallet":title_wallet_name(t),
                "wallet_id":wallet_info_by_title.get(t.id,{}).get("id")
            } for t in sorted(ar_open,key=lambda t:_date(t.due_date) or date.max)[:250]],
        },
        "payables":{
            "open":round(sum(max(_money(t.value)-_money(t.paid_value),0) for t in ap_open),2),"overdue":round(sum(max(_money(t.value)-_money(t.paid_value),0) for t in ap_overdue),2),"due":round(sum(max(_money(t.value)-_money(t.paid_value),0) for t in ap_open if t not in ap_overdue),2),
            "period":issued_payables,"paid":payable_realized,"forecast":outflows,
            "planned":payable_planned,"realized":payable_realized,"variance":payable_variance,"execution_pct":payable_execution_pct,
            "planned_count":len(ap_due_period) if payable_planned is not None else None,"realized_count":len(ap_paid_period) if payable_realized is not None else None,
            "paid_late":payable_paid_late if payable_realized is not None else None,"paid_late_count":payable_paid_late_count if payable_realized is not None else None,
            "overdue_count":len(ap_overdue),"aging":payable_aging_details,"overdue_aging":[r for r in payable_aging_details if r["name"]!="A vencer"],
            "short_cashflow":short_cashflow,
            "due_coverage_pct":payable_due_coverage_pct,"paid_date_coverage_pct":payable_paid_date_coverage_pct,
            "suppliers":payable_supplier_drill[:20],
            "overdue_suppliers":payable_overdue_supplier_drill[:20],
            "planned_titles":payable_planned_titles[:250],"realized_titles":payable_realized_titles[:250],"paid_late_titles":payable_paid_late_titles[:250],
            "expenses_total":issued_expenses,
            "titles":[{
                "id":t.id,"document":t.document,"partner":_partner_name(partners,t.partner_id),"due":t.due_date,"value":_money(t.value),
                "paid":_money(t.paid_value),"balance":round(max(_money(t.value)-_money(t.paid_value),0),2),
                "status":"Vencido" if _date(t.due_date) and _date(t.due_date)<today else ("A vencer" if _date(t.due_date) else "Sem vencimento"),
                "days_overdue":(today-_date(t.due_date)).days if _date(t.due_date) and _date(t.due_date)<today else 0,
            } for t in sorted(ap_open,key=lambda t:_date(t.due_date) or date.max)[:250]],
        },
        "cashflow":{"received":round(received_total,2),"paid":round(paid_total,2),"realized_net":round(received_total-paid_total,2),"projection":cash_projection_drill,"received_details":cash_received_details,"paid_details":cash_paid_details,"bank_balance":None},
        "dre":{
            "rows":dre_rows,"groups":dre_group_rows,"categories":dre_category_rows,"monthly":dre_monthly,"details":dre_expense_details,
            "sales":round(sales_total,2),"expenses":dre_expenses_total,"partial_result":dre_partial_result,
            "previous_sales":round(sales_prev_total,2),"previous_expenses":dre_expenses_prev,"previous_partial":dre_partial_prev,
            "partial_variation":_variation(dre_partial_result,dre_partial_prev) if expense_period_available else None,"unclassified":dre_unclassified,
            "unclassified_pct":(_pct(dre_unclassified,dre_expenses_total) or 0.0) if expense_period_available else None,"year":selected_year,"expense_period_available":expense_period_available,
            "groups_options":[{"code":code,"name":label} for code,label in DRE_GROUPS.items()],
            "limitations":[
                "Deduções, devoluções e tributos sobre vendas ainda não possuem uma base normalizada confiável para a DRE.",
                "CMV não é substituído por compras do período. É necessário custo efetivo/médio associado aos produtos vendidos.",
                "A DRE utiliza lançamentos do módulo Despesas; títulos de Contas a Pagar não são somados novamente para evitar duplicidade.",
                "O Resultado gerencial parcial não deve ser tratado como lucro contábil enquanto deduções, impostos e CMV estiverem indisponíveis.",
            ],
        },
        "commercial":{"sales_total":round(sum(seller_sales.values()),2),"previous_month_total":round(sum(seller_sales_previous_month.values()),2),"growth":_variation(sum(seller_sales.values()),sum(seller_sales_previous_month.values())),"sellers_count":len([r for r in seller_rows if r["value"]>0.005]),"orders":len(orders),"orders_value":round(sum(_money(o.total) for o in orders),2),"ticket":round(sum(_money(o.total) for o in orders)/len(orders),2) if orders else 0,"sellers":seller_dashboard[:50],"seller_categories":seller_category_rows[:100],"seller_category_groups":seller_category_groups[:50],"previous_month_start":previous_month_start,"previous_month_end":previous_month_end,"states":_rank(state_sales,limit=20),"customers_bought":len(bought_period),"new_customers":new_clients,"inactive_60":inactive_60,
                      "seller_diagnostics":{"total_invoices":len(sales),"identified":sum(v for k,v in seller_source_counts.items() if k != "não identificado"),"unidentified":seller_source_counts.get("não identificado",0),"sources":[{"name":k,"count":v} for k,v in sorted(seller_source_counts.items(),key=lambda kv:kv[1],reverse=True)]}},
        "customers":{"total":len(customer_ids),"bought":len(bought_period),"new":new_clients,"recovered":recovered,"inactive_30":inactive_30,"inactive_60":inactive_60,"inactive_90":inactive_90,"abc":customer_abc,"top_buyers":customer_rank[:50],"customer_category_groups":customer_category_groups[:50],"concentration":{"top5":concentration(5),"top10":concentration(10),"top20":concentration(20)}},
        "products":{"revenue":product_revenue_rows[:50],"qty":product_qty_rows[:50],"categories":_rank(cat_rev,limit=30),"subcategories":_rank(subcat_rev,limit=30),"drill_products":product_dashboard[:100],"category_drill":category_dashboard[:100],"margin_available":False,"inventory_available":False},
        "purchases":{"total":round(purchases_total,2),"docs":len(purchases),"ticket":round(purchases_total/len(purchases),2) if purchases else 0,"suppliers":purchase_supplier_drill[:30],"products":purchase_product_drill[:30],"documents":[{"id":x.id,"number":x.number,"date":x.issue_date,"partner":_partner_name(partners,x.supplier_id),"value":purchase_effective_values.get(x.id,0.0),"reconciliation":x.reconciliation_status} for x in sorted(purchases,key=lambda x:_date(x.issue_date) or date.min,reverse=True)[:100]]},
        "logistics":{"deliveries":len(deliveries),"volumes":round(sum(float(d.volumes or 0) for d in deliveries),2),"freight":round(freight,2),"freight_pct":_pct(freight,sales_total),"status":logistics_status_drill,"regions":logistics_region_drill},
        "alerts":alerts,"unavailable":unavailable,
    }
    return result


SECTIONS = {
    "visao-geral": "Visão Geral",
    "faturamento": "Faturamento",
    "contas-receber": "Contas a Receber",
    "contas-pagar": "Contas a Pagar",
    "fluxo-caixa": "Fluxo de Caixa",
    "dre": "DRE Gerencial",
    "vendedores": "Comercial — Vendedores",
    "clientes": "Comercial — Clientes",
    "produtos": "Produtos e Rentabilidade",
    "compras": "Compras e Fornecedores",
    "estoque-logistica": "Estoque e Logística",
    "alertas": "Alertas e Oportunidades",
}
