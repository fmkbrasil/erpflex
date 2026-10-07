from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from .connectors import ConnectorError, ERPFlexClient, NFStockClient
from .db import (
    Delivery, DeliveryAssignment, DeliveryRule, FinancialTitle, Partner, Product, PurchaseInvoice, PurchaseItem, RawRecord,
    Responsible, SalesInvoice, SalesInvoiceItem, SalesOrder, SessionLocal, SyncCursor, SyncExecutionLease, SyncPageIndex, SyncProgress, SyncRun
)
from .utils import (
    access_key_from, deep_find, digits, external_id, fnum, normalize_date, payload_hash,
    pick, safe_json, text
)
from .xml_parser import parse_nfe
from .logistics import classificar_regiao, regiao_especial, alerta_endereco
from .integration_settings import get_erpflex_settings, get_nfstock_settings

_sync_lock = threading.Lock()


def _acquire_execution_lease(company_id: int, source: str, owner: str, ttl_hours: int = 12) -> bool:
    """Adquire um lease persistente para impedir execuções concorrentes entre processos.

    O lock em memória continua útil dentro de um processo, mas não protege Web e
    Worker quando rodam em serviços Railway separados. O lease no banco cobre
    exatamente esse caso.
    """
    now = datetime.utcnow()
    expires = now + timedelta(hours=max(1, int(ttl_hours)))
    try:
        with SessionLocal() as db:
            stmt = (
                update(SyncExecutionLease)
                .where(
                    SyncExecutionLease.company_id == company_id,
                    SyncExecutionLease.source == source,
                    or_(SyncExecutionLease.expires_at <= now, SyncExecutionLease.owner == owner),
                )
                .values(owner=owner, acquired_at=now, expires_at=expires)
            )
            result = db.execute(stmt)
            if result.rowcount:
                db.commit()
                return True
            exists = db.scalar(select(SyncExecutionLease).where(
                SyncExecutionLease.company_id == company_id,
                SyncExecutionLease.source == source,
            ).limit(1))
            if exists:
                db.rollback()
                return False
            try:
                db.add(SyncExecutionLease(
                    company_id=company_id, source=source, owner=owner,
                    acquired_at=now, expires_at=expires,
                ))
                db.commit()
                return True
            except Exception:
                db.rollback()
                return False
    except Exception:
        return False


def _release_execution_lease(company_id: int, source: str, owner: str) -> None:
    try:
        with SessionLocal() as db:
            row = db.scalar(select(SyncExecutionLease).where(
                SyncExecutionLease.company_id == company_id,
                SyncExecutionLease.source == source,
                SyncExecutionLease.owner == owner,
            ).limit(1))
            if row:
                db.delete(row)
                db.commit()
    except Exception:
        pass


def _update_progress(run_id: int, *, percent: float | None = None, phase: str | None = None,
                     current: int | None = None, total: int | None = None, detail: str | None = None,
                     heartbeat_only: bool = False) -> None:
    """Atualiza o progresso persistente sem depender da sessão da tela.

    `heartbeat_only` mantém viva a indicação do worker durante chamadas HTTP longas
    sem alterar a etapa exibida.
    """
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            row = db.get(SyncProgress, run_id)
            if not row:
                row = SyncProgress(run_id=run_id, percent=0.0, heartbeat_at=now, stage_updated_at=now)
                db.add(row)
                db.flush()
            row.heartbeat_at = now
            if not heartbeat_only:
                changed = False
                if percent is not None:
                    value = max(0.0, min(100.0, float(percent)))
                    if abs(float(row.percent or 0.0) - value) >= 0.01:
                        row.percent = value
                        changed = True
                if phase is not None and row.phase != str(phase)[:120]:
                    row.phase = str(phase)[:120]
                    changed = True
                if current is not None and row.current != int(current):
                    row.current = int(current)
                    changed = True
                if total is not None and row.total != int(total):
                    row.total = int(total)
                    changed = True
                if detail is not None and row.detail != str(detail)[:500]:
                    row.detail = str(detail)[:500]
                    changed = True
                if changed:
                    row.stage_updated_at = now
            db.commit()
    except Exception:
        # Progresso é observabilidade; nunca pode derrubar a sincronização.
        pass


def _start_progress_heartbeat(run_id: int, interval: float = 5.0):
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            _update_progress(run_id, heartbeat_only=True)

    thread = threading.Thread(target=beat, name=f"sync-heartbeat-{run_id}", daemon=True)
    thread.start()
    return stop, thread


def _progress_from_message(message: str, event_no: int, limit: int) -> tuple[float, int | None, int | None, str]:
    """Converte mensagens do motor em um percentual estimado.

    As fases de descoberta/binária não têm total exato; nesses trechos a barra é
    deliberadamente estimada. Quando a mensagem traz X/Y, o cálculo passa a ser
    proporcional ao total real da fase.
    """
    import re

    msg = str(message or "")
    current = total = None
    m = re.search(r"(?:\(|·|bloco|detalhe)\s*(\d+)\s*/\s*(\d+)", msg, flags=re.I)
    if not m:
        m = re.search(r"(\d+)\s*/\s*(\d+)", msg)
    if m:
        current, total = int(m.group(1)), max(1, int(m.group(2)))
        ratio = max(0.0, min(1.0, current / total))
        if "cliente" in msg.lower():
            percent = 10.0 + ratio * 55.0
        elif "histórico" in msg.lower() or "detalhe" in msg.lower() or "bloco" in msg.lower():
            percent = 20.0 + ratio * 48.0
        else:
            percent = 15.0 + ratio * 50.0
    else:
        # Cresce devagar durante descoberta/localização, sem prometer precisão falsa.
        percent = min(58.0, 8.0 + min(max(1, event_no), max(5, limit)) * (50.0 / max(5, limit)))
    phase = "Consultando API"
    lower = msg.lower()
    if "fase 1/2" in lower or "última página" in lower or "ultima pagina" in lower:
        phase = "Encontrando última página válida"
    elif "fase 2/2" in lower or "trás para frente" in lower or "regressiva" in lower:
        phase = "Lendo período de trás para frente"
    elif "fallback histórico" in lower or "fallback historico" in lower:
        phase = "Localizando período antigo"
    elif "localizando" in lower:
        phase = "Localizando período"
    elif "borda" in lower:
        phase = "Validando última página"
    elif "histórico" in lower:
        phase = "Lendo período"
    elif "detalhe" in lower:
        phase = "Enriquecendo detalhes"
    elif "cliente" in lower:
        phase = "Enriquecendo clientes"
    elif "compras" in lower:
        phase = "Consultando compras"
    elif "faturamento" in lower:
        phase = "Consultando faturamento"
    return percent, current, total, phase


MOVEMENT_MODULES = {"orders", "faturamento", "receber", "pagar", "compras", "despesas"}
MASTER_MODULES = {"products", "banks", "clientes"}


def _get_sync_cursor(company_id: int, module: str) -> dict:
    with SessionLocal() as db:
        row = db.scalar(select(SyncCursor).where(
            SyncCursor.company_id == company_id,
            SyncCursor.source == "ERPFLEX",
            SyncCursor.module == module,
        ).limit(1))
        if not row:
            return {}
        return {
            "initialized": bool(row.initialized),
            "next_offset": row.next_offset,
            "last_valid": row.last_valid,
            "first_invalid": row.first_invalid,
            "coverage_from": row.coverage_from,
            "coverage_to": row.coverage_to,
            "coverage_complete": bool(row.coverage_complete),
            "last_http_requests": row.last_http_requests or 0,
        }


def _save_sync_cursor(company_id: int, module: str, meta: dict | None):
    if not meta:
        return
    with SessionLocal() as db:
        row = db.scalar(select(SyncCursor).where(
            SyncCursor.company_id == company_id,
            SyncCursor.source == "ERPFLEX",
            SyncCursor.module == module,
        ).limit(1))
        if not row:
            row = SyncCursor(company_id=company_id, source="ERPFLEX", module=module)
            db.add(row)
        for name in ("next_offset", "last_valid", "first_invalid", "coverage_from", "coverage_to", "last_http_requests"):
            if name in meta and meta.get(name) is not None:
                setattr(row, name, meta.get(name))
        if "initialized" in meta:
            row.initialized = bool(meta.get("initialized"))
        if "coverage_complete" in meta:
            row.coverage_complete = bool(meta.get("coverage_complete"))
        row.last_sync_at = datetime.utcnow()
        db.commit()


def _save_page_index(company_id: int, module: str, cursor_kind: str, cursor_value: int, records: list[dict]) -> None:
    """Persiste metadados de uma página/offset já percorrido.

    O índice não substitui os registros centrais e não interfere na gravação da
    sincronização. Ele apenas acelera a localização de períodos futuros.
    """
    if not records:
        return
    dates = [d for d in (_record_date(module, r) for r in records) if d]
    min_date = min(dates).isoformat() if dates else None
    max_date = max(dates).isoformat() if dates else None
    ids = [str(external_id(module, r) or "") for r in records]
    signature = payload_hash({"ids": ids, "dates": [min_date, max_date], "count": len(records)})
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.scalar(select(SyncPageIndex).where(
            SyncPageIndex.company_id == company_id,
            SyncPageIndex.source == "ERPFLEX",
            SyncPageIndex.module == module,
            SyncPageIndex.cursor_kind == cursor_kind,
            SyncPageIndex.cursor_value == int(cursor_value),
        ).limit(1))
        if not row:
            row = SyncPageIndex(
                company_id=company_id, source="ERPFLEX", module=module,
                cursor_kind=cursor_kind, cursor_value=int(cursor_value), changed_at=now,
            )
            db.add(row)
        elif row.content_hash != signature:
            row.changed_at = now
        row.min_date = min_date
        row.max_date = max_date
        row.first_external_id = ids[0][:160] if ids and ids[0] else None
        row.last_external_id = ids[-1][:160] if ids and ids[-1] else None
        row.record_count = len(records)
        row.content_hash = signature
        row.last_checked_at = now
        db.commit()


def _page_index_hint(company_id: int, module: str, start_date: str, end_date: str, cursor: dict | None = None) -> dict:
    if not start_date or not end_date:
        return {}
    try:
        start = date.fromisoformat(str(start_date)[:10]).isoformat()
        end = date.fromisoformat(str(end_date)[:10]).isoformat()
    except Exception:
        return {}
    if end < start:
        start, end = end, start
    with SessionLocal() as db:
        rows = db.scalars(select(SyncPageIndex).where(
            SyncPageIndex.company_id == company_id,
            SyncPageIndex.source == "ERPFLEX",
            SyncPageIndex.module == module,
            SyncPageIndex.min_date.is_not(None),
            SyncPageIndex.max_date.is_not(None),
            SyncPageIndex.max_date >= start,
            SyncPageIndex.min_date <= end,
        ).order_by(SyncPageIndex.cursor_value.asc()).limit(500)).all()
    if not rows:
        return {}
    coverage_to = str((cursor or {}).get("coverage_to") or "")[:10]
    return {
        "min_cursor": min(r.cursor_value for r in rows),
        "max_cursor": max(r.cursor_value for r in rows),
        "count": len(rows),
        "cursor_kind": rows[0].cursor_kind,
        "touches_recent": bool(coverage_to and end >= coverage_to),
        "indexed_from": min((r.min_date for r in rows if r.min_date), default=None),
        "indexed_to": max((r.max_date for r in rows if r.max_date), default=None),
    }


def page_index_summary(company_id: int) -> list[dict]:
    """Resumo citable pela tela de sincronização, sem expor payloads."""
    with SessionLocal() as db:
        modules = db.execute(select(
            SyncPageIndex.module,
            func.count(SyncPageIndex.id),
            func.min(SyncPageIndex.min_date),
            func.max(SyncPageIndex.max_date),
            func.max(SyncPageIndex.last_checked_at),
        ).where(
            SyncPageIndex.company_id == company_id, SyncPageIndex.source == "ERPFLEX"
        ).group_by(SyncPageIndex.module).order_by(SyncPageIndex.module)).all()
    return [{
        "module": m, "pages": int(c or 0), "from": mn, "to": mx, "last_checked_at": checked
    } for m, c, mn, mx, checked in modules]


def _record_date(module: str, record: dict) -> date | None:
    names = {
        "orders": ("emissao", "data_inclusao", "dt_inclusao"),
        "faturamento": ("data_emissao", "dt_inclusao"),
        "receber": ("vencimento", "data_baixa"),
        "pagar": ("vencimento", "data_baixa"),
        "compras": ("emissao", "emissao_original", "data_saida"),
        "despesas": ("data_emissao",),
    }.get(module, ("data", "emissao", "data_emissao"))
    value = pick(record, *names)
    if value in (None, ""):
        value = deep_find(record, names)
    normalized = normalize_date(value)
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized[:10])
    except Exception:
        return None


def _filter_period(module: str, records: list[dict], start_date: str | None, end_date: str | None) -> list[dict]:
    if module not in MOVEMENT_MODULES or not start_date or not end_date:
        return records
    try:
        start = date.fromisoformat(str(start_date)[:10])
        end = date.fromisoformat(str(end_date)[:10])
    except Exception:
        return records
    if end < start:
        start, end = end, start
    selected = []
    for record in records:
        d = _record_date(module, record)
        if d and start <= d <= end:
            selected.append(record)
    return selected


def _known_customer_ids(company_id: int, limit: int = 1000) -> list[str]:
    """IDs de clientes já observados em movimentos sincronizados.

    O único endpoint de cliente já validado no projeto é o detalhe individual.
    Por isso enriquecemos os clientes conhecidos sem inventar um endpoint em lote.
    """
    ids: set[str] = set()
    with SessionLocal() as db:
        partners = db.scalars(select(Partner).where(
            Partner.company_id == company_id,
            Partner.role_customer.is_(True),
            Partner.erpflex_id.is_not(None),
        ).limit(limit)).all()
        ids.update(str(p.erpflex_id) for p in partners if p.erpflex_id)
        if len(ids) < limit:
            raws = db.scalars(select(RawRecord).where(
                RawRecord.company_id == company_id,
                RawRecord.source == "ERPFLEX",
                RawRecord.module.in_(["orders", "faturamento", "receber"]),
            ).order_by(RawRecord.id.desc()).limit(max(limit * 5, 1000))).all()
            for raw in raws:
                try:
                    rec = json.loads(raw.payload_json or "{}")
                except Exception:
                    continue
                cid = deep_find(rec, ("cliente_id", "id_cliente"))
                if cid not in (None, ""):
                    ids.add(str(cid))
                if len(ids) >= limit:
                    break
    return sorted(ids)[:limit]


def _partner(db, company_id: int, role: str, record: dict, *, nf_xml: dict | None = None):
    if nf_xml and role == "supplier":
        cnpj = digits(nf_xml.get("issuer_cnpj"))
        name = text(nf_xml.get("issuer_name"))
        trade = None
    elif nf_xml and role == "customer":
        cnpj = digits(nf_xml.get("recipient_cnpj"))
        name = text(nf_xml.get("recipient_name"))
        trade = None
    else:
        if role == "supplier":
            cnpj = digits(deep_find(record, ("fornecedor_cnpj", "cnpj_fornecedor", "cpf_cnpj_fornecedor", "cnpj", "documento_fornecedor")))
            name = text(deep_find(record, ("fornecedor_desc", "fornecedor", "fornecedor_nome", "razao_social", "cliente", "nome_fornecedor", "nome")))
            trade = text(deep_find(record, ("nome_fantasia", "fantasia", "fornecedor_fantasia"))) or None
        else:
            cnpj = digits(deep_find(record, ("cliente_cnpj", "cnpj_cliente", "cpf_cnpj", "cnpj", "documento_cliente")))
            name = text(deep_find(record, ("cliente_desc", "cliente", "cliente_nome", "razao_social", "nome_cliente", "nome")))
            trade = text(deep_find(record, ("nome_fantasia", "fantasia", "cliente_fantasia"))) or None
    cnpj = cnpj or None
    row = None
    if cnpj:
        row = db.scalar(select(Partner).where(Partner.company_id == company_id, Partner.cnpj_cpf == cnpj).limit(1))
    if not row and name:
        row = db.scalar(select(Partner).where(Partner.company_id == company_id, func.lower(Partner.name) == name.lower()).limit(1))
    if not row:
        row = Partner(company_id=company_id, cnpj_cpf=cnpj, name=name or "Não identificado", trade_name=trade)
        db.add(row)
        db.flush()
    else:
        if name and (not row.name or row.name == "Não identificado"):
            row.name = name
        if trade:
            row.trade_name = trade
        if cnpj and not row.cnpj_cpf:
            row.cnpj_cpf = cnpj
    if role == "supplier":
        row.role_supplier = True
        peid = deep_find(record, ("fornecedor_id", "id_fornecedor"))
    else:
        row.role_customer = True
        peid = deep_find(record, ("cliente_id", "id_cliente"))
    if peid not in (None, "") and not row.erpflex_id:
        row.erpflex_id = str(peid)
    # Dados de endereço quando claramente disponíveis.
    row.address = row.address or text(deep_find(record, ("endereco", "logradouro", "endereco_entrega"))) or None
    row.district = row.district or text(deep_find(record, ("bairro", "bairro_entrega"))) or None
    row.city = row.city or text(deep_find(record, ("municipio", "cidade", "cidade_entrega"))) or None
    row.state = row.state or text(deep_find(record, ("uf", "estado", "uf_entrega"))) or None
    row.zip_code = row.zip_code or text(deep_find(record, ("cep", "cep_entrega"))) or None
    return row


def _raw_upsert(db, company_id: int, source: str, module: str, record: dict):
    ext = external_id(module if source == "ERPFLEX" else "nfstock", record)
    h = payload_hash(record)
    row = db.scalar(select(RawRecord).where(
        RawRecord.company_id == company_id,
        RawRecord.source == source,
        RawRecord.module == module,
        RawRecord.external_id == ext,
    ).limit(1))
    inserted = False
    if not row:
        row = RawRecord(company_id=company_id, source=source, module=module, external_id=ext,
                        record_hash=h, payload_json=safe_json(record))
        db.add(row)
        db.flush()
        inserted = True
    elif row.record_hash != h:
        row.record_hash = h
        row.payload_json = safe_json(record)
        row.updated_at = datetime.utcnow()
    return row, inserted


def _erp_product(db, company_id: int, record: dict):
    eid = str(deep_find(record, ("id", "id_produto", "codigo_produto", "codigo", "sku")) or external_id("products", record))
    row = db.scalar(select(Product).where(Product.company_id == company_id, Product.erpflex_id == eid).limit(1))
    if not row:
        row = Product(company_id=company_id, erpflex_id=eid)
        db.add(row)
    row.code = text(deep_find(record, ("codigo", "codigo_produto", "sku", "referencia"))) or row.code
    row.description = text(deep_find(record, ("produto", "descricao", "nome", "descricao_produto"))) or row.description
    row.ean = text(deep_find(record, ("ean", "gtin", "codigo_barras"))) or row.ean
    row.ncm = text(deep_find(record, ("ncm", "codigo_ncm"))) or row.ncm
    row.unit = text(deep_find(record, ("unidade", "un", "unidade_medida"))) or row.unit
    row.category = text(deep_find(record, ("categoria", "grupo", "categoria_produto"))) or row.category


def _erp_order(db, company_id: int, record: dict, raw_id: int):
    eid = str(deep_find(record, ("id", "solicitacao_id", "pedido_id", "id_solicitacao", "numero_pedido", "pedido", "codigo")) or external_id("orders", record))
    row = db.scalar(select(SalesOrder).where(SalesOrder.company_id == company_id, SalesOrder.erpflex_id == eid).limit(1))
    if not row:
        row = SalesOrder(company_id=company_id, erpflex_id=eid)
        db.add(row)
    customer = _partner(db, company_id, "customer", record)
    row.number = text(deep_find(record, ("numero_pedido", "pedido", "numero", "documento"))) or row.number
    row.customer_id = customer.id if customer else row.customer_id
    row.issue_date = normalize_date(deep_find(record, ("emissao", "data_emissao", "data_inclusao", "data"))) or row.issue_date
    row.forecast_date = normalize_date(deep_find(record, ("previsao_faturamento", "previsao_entrega", "data_entrega", "data_previsao"))) or row.forecast_date
    row.status = text(deep_find(record, ("status", "situacao", "estado"))) or row.status
    row.total = fnum(deep_find(record, ("valor_total", "total", "valor", "valor_pedido"))) or row.total
    row.raw_record_id = raw_id




def _sales_items_for_storage(record: dict) -> list[dict]:
    raw = _purchase_items_from_record(record or {})
    if not raw:
        return []
    out = []
    for item in raw:
        out.append({
            "item_id": text(_purchase_item_value(item, ("item_id", "id_item", "id"))),
            "product_id": text(_purchase_item_value(item, ("produto_id", "id_produto", "product_id"))),
            "code": text(_purchase_item_value(item, ("codigo_produto", "produto_codigo", "codigo", "code", "sku", "referencia"))),
            "ean": text(_purchase_item_value(item, ("EAN", "ean", "gtin", "codigo_barras", "cEAN"))),
            "description": text(_purchase_item_value(item, ("desc_produto", "descricao_produto", "descricao_item", "produto_desc", "produto", "descricao", "description", "nome"))),
            "ncm": text(_purchase_item_value(item, ("ncm", "NCM"))),
            "cfop": text(_purchase_item_value(item, ("cfop", "CFOP"))),
            "unit": text(_purchase_item_value(item, ("unidade", "unit", "uCom"))),
            "qty": fnum(_purchase_item_value(item, ("quantidade", "qtd", "qty", "qCom"))),
            "unit_price": fnum(_purchase_item_value(item, ("preco_unitario", "valor_unitario", "unit_price", "vUnCom", "unitario"))),
            "total": fnum(_purchase_item_value(item, ("preco_total_item", "valor_item", "valor_total", "total", "vProd"))),
            "icms": fnum(_purchase_item_value(item, ("valor_icms", "icms", "vICMS"))),
            "ipi": fnum(_purchase_item_value(item, ("valor_ipi", "ipi", "vIPI"))),
        })
    return out


def _persist_sales_items(db, company_id: int, invoice: SalesInvoice, record: dict) -> int:
    items = _sales_items_for_storage(record)
    if not items:
        return 0
    db.query(SalesInvoiceItem).filter(SalesInvoiceItem.sales_invoice_id == invoice.id).delete(synchronize_session=False)
    for item in items:
        product = None
        pid, code, ean = item["product_id"], item["code"], item["ean"]
        conds = []
        if pid: conds.append(Product.erpflex_id == pid)
        if code: conds.append(Product.code == code)
        if ean: conds.append(Product.ean == ean)
        if conds:
            product = db.scalar(select(Product).where(Product.company_id == company_id, or_(*conds)).order_by(Product.id.desc()).limit(1))
        description = item["description"] or (product.description if product else "")
        db.add(SalesInvoiceItem(
            sales_invoice_id=invoice.id, erpflex_item_id=item["item_id"] or None,
            product_id=pid or None, product_code=code or (product.code if product else None),
            ean=ean or (product.ean if product else None), description=description or "",
            ncm=item["ncm"] or (product.ncm if product else None), cfop=item["cfop"] or None,
            unit=item["unit"] or (product.unit if product else None), qty=item["qty"],
            unit_price=item["unit_price"], total=item["total"], icms=item["icms"], ipi=item["ipi"],
        ))
    return len(items)


def _apply_delivery_rule(db, company_id: int, delivery: Delivery, customer: Partner | None):
    """Aplica apenas campos logísticos vazios; nunca sobrescreve decisão operacional já tomada."""
    if not customer:
        return
    rule = None
    if customer.cnpj_cpf:
        rule = db.scalar(select(DeliveryRule).where(
            DeliveryRule.company_id == company_id,
            DeliveryRule.active.is_(True),
            DeliveryRule.customer_cnpj == customer.cnpj_cpf,
        ).order_by(DeliveryRule.id.desc()).limit(1))
    if not rule and customer.name:
        rule = db.scalar(select(DeliveryRule).where(
            DeliveryRule.company_id == company_id,
            DeliveryRule.active.is_(True),
            func.lower(DeliveryRule.customer_name) == customer.name.lower(),
        ).order_by(DeliveryRule.id.desc()).limit(1))
    if not rule:
        return
    if rule.region and not delivery.region:
        delivery.region = rule.region
    if rule.box_type and not delivery.box_type:
        delivery.box_type = rule.box_type
    if rule.responsible_id:
        current = db.scalar(select(DeliveryAssignment).where(DeliveryAssignment.delivery_id == delivery.id).limit(1))
        if not current:
            resp = db.get(Responsible, rule.responsible_id)
            if resp and resp.active:
                db.add(DeliveryAssignment(company_id=company_id, delivery_id=delivery.id, responsible_id=resp.id))
                delivery.responsible = resp.name
                delivery.responsible_type = resp.kind

def _erp_sales_invoice(db, company_id: int, record: dict, raw_id: int):
    eid = str(deep_find(record, ("faturamento_id", "id", "id_faturamento", "id_nota", "nfe", "documento", "codigo_autenticacao_digital")) or external_id("faturamento", record))
    access_key = access_key_from(record)
    # Número da NF-e é campo fiscal específico. "documento" no ERPFlex pode ser o pedido/documento comercial.
    number = text(deep_find(record, ("nr_nfe", "nfe", "numero_nfe", "numero_nf", "numero_nota", "nota_fiscal", "SF2_NrNfe")))
    row = db.scalar(select(SalesInvoice).where(SalesInvoice.company_id == company_id, SalesInvoice.erpflex_id == eid).limit(1))
    if not row and access_key:
        row = db.scalar(select(SalesInvoice).where(SalesInvoice.company_id == company_id, SalesInvoice.access_key == access_key).limit(1))
    # Contingência Excel e ERPFlex precisam convergir para a mesma NF central.
    # Quando não há chave, reaproveitamos a única NF com o mesmo número.
    if not row and number:
        candidates = db.scalars(select(SalesInvoice).where(SalesInvoice.company_id == company_id, SalesInvoice.number == number).limit(2)).all()
        if len(candidates) == 1:
            row = candidates[0]
    if not row:
        row = SalesInvoice(company_id=company_id, erpflex_id=eid)
        db.add(row)
        db.flush()
    elif row.erpflex_id != eid and (not row.erpflex_id or str(row.erpflex_id).startswith("EXCEL:")):
        row.erpflex_id = eid

    customer = _partner(db, company_id, "customer", record)
    row.access_key = access_key or row.access_key
    row.number = number or row.number
    row.series = text(deep_find(record, ("serie_da_nf", "serie", "serie_nf"))) or row.series
    row.customer_id = customer.id if customer else row.customer_id
    row.issue_date = normalize_date(deep_find(record, ("emissao", "emissao_original", "data_emissao", "data"))) or row.issue_date
    row.forecast_date = normalize_date(deep_find(record, ("previsao_faturamento", "previsao_entrega", "data_entrega"))) or row.forecast_date
    row.total = fnum(deep_find(record, ("valor_nf", "valor_total_da_nota", "valor_total", "total_nf", "total", "valor"))) or row.total
    row.carrier = text(deep_find(record, ("transportadora", "transportadora1", "transportador"))) or row.carrier
    row.carrier2 = text(deep_find(record, ("transportadora2", "modalidade", "tipo_entrega"))) or row.carrier2
    row.delivery_address = text(deep_find(record, ("endereco_entrega", "logradouro_entrega", "endereco"))) or row.delivery_address
    row.delivery_district = text(deep_find(record, ("bairro_entrega", "bairro"))) or row.delivery_district
    row.delivery_city = text(deep_find(record, ("municipio_entrega", "cidade_entrega", "municipio", "cidade"))) or row.delivery_city
    row.delivery_state = text(deep_find(record, ("uf_entrega", "uf", "estado"))) or row.delivery_state
    row.delivery_zip = text(deep_find(record, ("cep_entrega", "cep"))) or row.delivery_zip
    row.volumes = fnum(deep_find(record, ("quantidade_volume", "qtd_volume", "volumes", "volume"))) or row.volumes
    row.raw_record_id = raw_id
    db.flush()
    _persist_sales_items(db, company_id, row, record)

    delivery = db.scalar(select(Delivery).where(Delivery.sales_invoice_id == row.id).limit(1))
    if not delivery:
        delivery = Delivery(company_id=company_id, sales_invoice_id=row.id, volumes=row.volumes or 0, box_type="Papelão")
        db.add(delivery)
        db.flush()
    elif row.volumes and not delivery.volumes:
        delivery.volumes = row.volumes

    _apply_delivery_rule(db, company_id, delivery, customer)
    canal = text(deep_find(record, ("canal", "canal_venda", "canalvenda")))
    special = regiao_especial(row.carrier, row.carrier2)
    if special and (not delivery.region or delivery.region == "A Classificar"):
        delivery.region = special
    elif not delivery.region or delivery.region == "A Classificar":
        delivery.region = classificar_regiao(row.delivery_city, row.delivery_district, row.delivery_zip, canal)

    alert = alerta_endereco(row.carrier, row.carrier2)
    if alert and alert not in (delivery.notes or "").upper():
        delivery.notes = ((delivery.notes or "") + (" | " if delivery.notes else "") + alert).strip()

    if delivery.region == "COLETA":
        current = db.scalar(select(DeliveryAssignment).where(DeliveryAssignment.delivery_id == delivery.id).limit(1))
        if not current:
            coleta = db.scalar(select(Responsible).where(Responsible.company_id == company_id, Responsible.name == "COLETA", Responsible.active.is_(True)).limit(1))
            if coleta:
                db.add(DeliveryAssignment(company_id=company_id, delivery_id=delivery.id, responsible_id=coleta.id))
                delivery.responsible = coleta.name
                delivery.responsible_type = coleta.kind


def _purchase_items_from_record(record: dict) -> list[dict]:
    """Extrai itens de compra inclusive em envelopes/listas aninhados.

    Prioriza arrays semanticamente chamados produtos/itens, mas aceita estruturas
    equivalentes quando os objetos possuem campos típicos de item.
    """
    candidates: list[tuple[int, list[dict]]] = []
    hints = ("produtos", "itens", "items", "produtos_itens", "detalhes", "detalhe")

    def walk(value, path: str = ""):
        if isinstance(value, dict):
            for key, child in value.items():
                lk = str(key).lower()
                if isinstance(child, list):
                    rows = [x for x in child if isinstance(x, dict)]
                    if rows:
                        score = 20 if lk in hints else 0
                        sample = rows[:5]
                        for it in sample:
                            keys = " ".join(str(k).lower() for k in it.keys())
                            score += sum(3 for h in ("produto", "descricao", "quant", "preco", "valor_item", "ncm", "cfop") if h in keys)
                        if score > 0:
                            candidates.append((score + min(len(rows), 10), rows))
                walk(child, f"{path}.{lk}" if path else lk)
        elif isinstance(value, list):
            for child in value[:50]:
                walk(child, path)

    walk(record)
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _purchase_item_value(item: dict, names: tuple[str, ...]):
    value = pick(item, *names)
    if value in (None, ""):
        value = deep_find(item, names)
    return value


def _merge_purchase_rows(db, keep: PurchaseInvoice, remove: PurchaseInvoice):
    """Mescla duas linhas que representam a mesma NF sem perder origem/XML/itens."""
    if keep.id == remove.id:
        return keep
    keep.access_key = keep.access_key or remove.access_key
    keep.erpflex_id = keep.erpflex_id or remove.erpflex_id
    keep.nfstock_nsu = keep.nfstock_nsu or remove.nfstock_nsu
    keep.number = keep.number or remove.number
    keep.series = keep.series or remove.series
    keep.supplier_id = keep.supplier_id or remove.supplier_id
    keep.issue_date = keep.issue_date or remove.issue_date
    keep.total_erpflex = keep.total_erpflex or remove.total_erpflex
    keep.total_nfstock = keep.total_nfstock or remove.total_nfstock
    keep.xml_text = keep.xml_text or remove.xml_text
    keep.erpflex_raw_id = keep.erpflex_raw_id or remove.erpflex_raw_id
    keep.nfstock_raw_id = keep.nfstock_raw_id or remove.nfstock_raw_id
    db.query(PurchaseItem).filter(PurchaseItem.purchase_invoice_id == remove.id).update(
        {PurchaseItem.purchase_invoice_id: keep.id}, synchronize_session=False
    )
    db.delete(remove)
    db.flush()
    _reconcile(keep)
    return keep


def _erp_purchase(db, company_id: int, record: dict, raw_id: int):
    eid = str(deep_find(record, ("id", "compra_id", "id_compra", "id_informacoes_fiscais", "codigo", "documento", "nfe")) or external_id("compras", record))
    key = access_key_from(record)
    row_by_key = db.scalar(select(PurchaseInvoice).where(PurchaseInvoice.company_id == company_id, PurchaseInvoice.access_key == key).limit(1)) if key else None
    row_by_eid = db.scalar(select(PurchaseInvoice).where(PurchaseInvoice.company_id == company_id, PurchaseInvoice.erpflex_id == eid).limit(1))
    if row_by_key and row_by_eid and row_by_key.id != row_by_eid.id:
        row = _merge_purchase_rows(db, row_by_key, row_by_eid)
    else:
        row = row_by_key or row_by_eid
    if not row:
        row = PurchaseInvoice(company_id=company_id, erpflex_id=eid, access_key=key)
        db.add(row)
        db.flush()
    else:
        row.erpflex_id = eid
        row.access_key = key or row.access_key
    supplier = _partner(db, company_id, "supplier", record)
    row.supplier_id = supplier.id if supplier else row.supplier_id
    row.number = text(deep_find(record, ("nfe", "documento", "numero_nota", "numero"))) or row.number
    row.series = text(deep_find(record, ("serie_da_nf", "serie", "serie_nf"))) or row.series
    row.issue_date = normalize_date(deep_find(record, ("emissao", "emissao_original", "data_emissao", "data"))) or row.issue_date
    row.total_erpflex = fnum(deep_find(record, ("valor_total_da_nota", "valor_total", "total", "valor"))) or row.total_erpflex
    row.erpflex_raw_id = raw_id

    # O endpoint /api/compra/confirmado/{id} retorna o detalhe da compra,
    # inclusive os itens. Quando os itens existem, atualiza o espelho local para
    # que a tela de resumo/ impressão tenha a nota completa.
    items = _purchase_items_from_record(record)
    if items:
        db.query(PurchaseItem).filter(PurchaseItem.purchase_invoice_id == row.id).delete(synchronize_session=False)
        for item in items:
            product_id = text(_purchase_item_value(item, ("produto_id", "id_produto", "product_id")))
            product_code = text(_purchase_item_value(item, ("codigo_produto", "produto_codigo", "codigo", "sku", "referencia")))
            ean = text(_purchase_item_value(item, ("EAN", "ean", "gtin", "codigo_barras", "cEAN")))
            description = text(_purchase_item_value(item, ("desc_produto", "descricao_produto", "descricao_item", "produto_desc", "produto", "descricao", "nome", "natureza_descricao")))
            product = None
            conds = []
            if product_id: conds.append(Product.erpflex_id == product_id)
            if product_code: conds.append(Product.code == product_code)
            if ean: conds.append(Product.ean == ean)
            if conds:
                product = db.scalar(select(Product).where(Product.company_id == company_id, or_(*conds)).order_by(Product.id.desc()).limit(1))
            db.add(PurchaseItem(
                purchase_invoice_id=row.id,
                product_code=product_code or product_id or (product.code if product else None),
                ean=ean or (product.ean if product else None),
                description=description or (product.description if product else ""),
                ncm=text(_purchase_item_value(item, ("ncm", "NCM"))) or (product.ncm if product else None),
                cfop=text(_purchase_item_value(item, ("cfop", "CFOP"))) or None,
                unit=text(_purchase_item_value(item, ("unidade", "unit", "uCom"))) or (product.unit if product else None),
                qty=fnum(_purchase_item_value(item, ("quantidade", "qtd", "qCom"))),
                unit_price=fnum(_purchase_item_value(item, ("preco_unitario", "valor_unitario", "vUnCom", "unitario"))),
                total=fnum(_purchase_item_value(item, ("preco_total_item", "valor_item", "valor_total", "total", "vProd"))),
                icms=fnum(_purchase_item_value(item, ("valor_icms", "icms", "vICMS"))),
                icms_st=fnum(_purchase_item_value(item, ("valor_icmsst", "icms_st", "vICMSST"))),
                ipi=fnum(_purchase_item_value(item, ("valor_ipi", "ipi", "vIPI"))),
                pis=fnum(_purchase_item_value(item, ("valor_pis", "pis", "vPIS"))),
                cofins=fnum(_purchase_item_value(item, ("valor_cofins", "cofins", "vCOFINS"))),
            ))
    _reconcile(row)


def _financial(db, company_id: int, kind: str, record: dict, raw_id: int):
    module = "receber" if kind == "RECEBER" else "pagar" if kind == "PAGAR" else "despesas"
    eid = str(deep_find(record, ("id", "id_titulo", "id_receita", "id_despesa", "documento", "numero")) or external_id(module, record))
    row = db.scalar(select(FinancialTitle).where(
        FinancialTitle.company_id == company_id, FinancialTitle.kind == kind, FinancialTitle.erpflex_id == eid
    ).limit(1))
    if not row:
        row = FinancialTitle(company_id=company_id, kind=kind, erpflex_id=eid)
        db.add(row)
    role = "customer" if kind == "RECEBER" else "supplier"
    partner = _partner(db, company_id, role, record)
    row.partner_id = partner.id if partner else row.partner_id
    # Documento permanece o identificador comercial/título; não usar nr_nfe como fallback.
    row.document = text(deep_find(record, ("documento", "numero", "titulo", "numero_pedido", "pedido"))) or row.document
    row.issue_date = normalize_date(deep_find(record, ("emissao", "data_emissao", "data"))) or row.issue_date
    row.due_date = normalize_date(deep_find(record, ("vencimento", "data_vencimento", "dt_vencimento"))) or row.due_date
    row.paid_date = normalize_date(deep_find(record, ("pagamento", "data_pagamento", "baixa", "data_baixa"))) or row.paid_date
    row.value = fnum(deep_find(record, ("valor", "valor_titulo", "valor_total", "total"))) or row.value
    row.paid_value = fnum(deep_find(record, ("valor_pago", "valor_baixado", "valor_recebido"))) or row.paid_value
    row.status = text(deep_find(record, ("status", "situacao"))) or row.status
    bank_desc = text(deep_find(record, ("banco_desc", "nome_banco", "banco")))
    bank_id = text(deep_find(record, ("banco_id", "id_banco", "idBanco")))
    wallet_desc = text(deep_find(record, ("carteira_desc", "nome_carteira", "carteira")))
    wallet_id = text(deep_find(record, ("id_carteira", "carteira_id", "idCarteira")))
    row.bank = bank_desc or row.bank or (("Sem banco informado" if bank_id in {"", "0"} else f"Banco {bank_id}") if bank_id or not row.bank else row.bank)
    row.wallet = wallet_desc or row.wallet or (("Sem carteira identificada" if wallet_id in {"", "0"} else f"Carteira {wallet_id}") if wallet_id or not row.wallet else row.wallet)
    row.raw_record_id = raw_id


def _reconcile(row: PurchaseInvoice):
    has_erp = bool(row.erpflex_id)
    has_nf = bool(row.nfstock_nsu or row.xml_text)
    if has_erp and has_nf:
        if row.total_erpflex and row.total_nfstock and abs(row.total_erpflex - row.total_nfstock) > 0.01:
            row.reconciliation_status = "DIVERGENTE_VALOR"
        else:
            row.reconciliation_status = "CONCILIADO"
    elif has_erp:
        row.reconciliation_status = "SOMENTE_ERPFLEX"
    elif has_nf:
        row.reconciliation_status = "SOMENTE_NFSTOCK"
    else:
        row.reconciliation_status = "PENDENTE"


def _save_erp_records(run_id: int, module: str, records: list[dict]):
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        if not run:
            return
        company_id = run.company_id
        run.total_found = len(records)
        progress = db.get(SyncProgress, run_id)
        if not progress:
            progress = SyncProgress(run_id=run_id, percent=70.0, phase="Processando registros", current=0, total=len(records), detail=f"0/{len(records)}", heartbeat_at=datetime.utcnow(), stage_updated_at=datetime.utcnow())
            db.add(progress)
        else:
            progress.percent = 70.0
            progress.phase = "Processando registros"
            progress.current = 0
            progress.total = len(records)
            progress.detail = f"0/{len(records)}"
            progress.heartbeat_at = datetime.utcnow()
            progress.stage_updated_at = datetime.utcnow()
        db.commit()
        for idx, record in enumerate(records, 1):
            try:
                raw, inserted = _raw_upsert(db, company_id, "ERPFLEX", module, record)
                if module == "products":
                    _erp_product(db, company_id, record)
                elif module == "clientes":
                    partner = _partner(db, company_id, "customer", record)
                    cid = deep_find(record, ("id", "id_cliente", "cliente_id"))
                    if cid not in (None, ""):
                        partner.erpflex_id = str(cid)
                elif module == "banks":
                    # Cadastro preservado integralmente em raw_records; nenhuma estrutura é inventada.
                    pass
                elif module == "orders":
                    _erp_order(db, company_id, record, raw.id)
                elif module == "faturamento":
                    _erp_sales_invoice(db, company_id, record, raw.id)
                elif module == "compras":
                    _erp_purchase(db, company_id, record, raw.id)
                elif module == "receber":
                    _financial(db, company_id, "RECEBER", record, raw.id)
                elif module == "pagar":
                    _financial(db, company_id, "PAGAR", record, raw.id)
                elif module == "despesas":
                    _financial(db, company_id, "DESPESA", record, raw.id)
                run.total_inserted += 1 if inserted else 0
                run.total_updated += 0 if inserted else 1
                run.total_processed = idx
                run.current_step = f"Processando {idx}/{len(records)}"
                if progress:
                    progress.percent = 70.0 + (28.0 * idx / max(1, len(records)))
                    progress.phase = "Processando registros"
                    progress.current = idx
                    progress.total = len(records)
                    progress.detail = run.current_step
                    progress.heartbeat_at = datetime.utcnow()
                    progress.stage_updated_at = datetime.utcnow()
                if idx % 20 == 0:
                    db.commit()
            except Exception as e:
                run.total_errors += 1
                run.message = (run.message or "") + f"\nRegistro {idx}: {e}"
        run.status = "CONCLUIDO" if run.total_errors == 0 else "CONCLUIDO_COM_ERROS"
        run.finished_at = datetime.utcnow()
        run.current_step = "Concluído"
        if progress:
            progress.percent = 100.0
            progress.phase = "Concluído"
            progress.current = len(records)
            progress.total = len(records)
            progress.detail = f"{len(records)} registro(s) processado(s)"
            progress.heartbeat_at = datetime.utcnow()
            progress.stage_updated_at = datetime.utcnow()
        db.commit()


def _run_erp(run_id: int, module: str, params: dict):
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        run.status = "EM_EXECUCAO"
        run.current_step = "Consultando ERPFlex · motor V7.8"
        db.commit()
    _update_progress(run_id, percent=3.0, phase="Iniciando", current=0, total=0, detail="Preparando sincronização ERPFlex")
    heartbeat_stop, heartbeat_thread = _start_progress_heartbeat(run_id)
    try:
        with SessionLocal() as db:
            run = db.get(SyncRun, run_id)
            settings = get_erpflex_settings(db, run.company_id)
            company_id = run.company_id
        movement_limit = max(1, min(1000, int(params.get("max_blocks") or 100)))
        master_limit = max(1, min(2000, int(params.get("master_max_pages") or 500)))
        limit = master_limit if module == "products" else movement_limit
        mode = str(params.get("mode") or "recent").lower()
        force_period = mode == "force_period"
        recent = mode not in {"manual", "force_period"}
        start_date = str(params.get("start_date") or "")
        end_date = str(params.get("end_date") or "")
        cursor = _get_sync_cursor(company_id, module)
        cursor_meta: dict = {}
        page_hint = _page_index_hint(company_id, module, start_date, end_date, cursor) if module in MOVEMENT_MODULES else {}

        progress_events = [0]
        def _progress(message: str):
            progress_events[0] += 1
            pct, cur, tot, phase = _progress_from_message(message, progress_events[0], limit)
            _update_progress(run_id, percent=pct, phase=phase, current=cur, total=tot, detail=message)
            with SessionLocal() as pdb:
                prun = pdb.get(SyncRun, run_id)
                if prun and prun.status == "EM_EXECUCAO":
                    prun.current_step = str(message)[:255]
                    pdb.commit()

        def _page_observer(obs_module: str, cursor_kind: str, cursor_value: int, records: list[dict]):
            _save_page_index(company_id, obs_module, cursor_kind, cursor_value, records)

        with ERPFlexClient(settings, progress=_progress, page_observer=_page_observer) as client:
            if module == "products":
                records = client.products(limit)
            elif module == "banks":
                records = client.banks()
            elif module == "clientes":
                max_clients = max(1, min(5000, int(params.get("max_clients") or 1000)))
                ids = _known_customer_ids(company_id, max_clients)
                records = []
                with SessionLocal() as db:
                    run = db.get(SyncRun, run_id)
                    run.total_found = len(ids)
                    run.current_step = f"Enriquecendo {len(ids)} cliente(s) conhecido(s)"
                    db.commit()
                for idx, cid in enumerate(ids, 1):
                    try:
                        detail = client.customer_detail(cid)
                        if detail:
                            records.append(detail)
                    except Exception as e:
                        with SessionLocal() as db:
                            run = db.get(SyncRun, run_id)
                            run.total_errors += 1
                            run.message = (run.message or "") + f"\nCliente {cid}: {e}"
                            db.commit()
                    if idx == 1 or idx % 25 == 0 or idx == len(ids):
                        _update_progress(run_id, percent=10.0 + 55.0 * idx / max(1, len(ids)), phase="Enriquecendo clientes", current=idx, total=len(ids), detail=f"Consultando cliente {idx}/{len(ids)}")
                        with SessionLocal() as db:
                            run = db.get(SyncRun, run_id)
                            run.current_step = f"Consultando cliente {idx}/{len(ids)}"
                            db.commit()
            elif module == "orders":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records, cursor_meta = client.orders_period(
                        start_date, end_date, limit,
                        end_offset=cursor.get("next_offset") if cursor.get("initialized") else None,
                        page_hint=page_hint or None,
                    )
                else:
                    records = client.orders(limit, None if recent else int(params.get("start_offset") or 0))
            elif module == "faturamento":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records, cursor_meta = client.faturamento_period(
                        start_date, end_date, limit,
                        last_valid=cursor.get("last_valid") if cursor.get("initialized") else None,
                        page_hint=page_hint or None, force_cached_range=bool(page_hint) and (force_period or not page_hint.get("touches_recent")),
                    )
                else:
                    records = client.faturamento(
                        limit,
                        None if recent else int(params.get("start_page") or 1),
                        last_valid=cursor.get("last_valid") if cursor.get("initialized") else None,
                    )
                if str(params.get("sales_details") or "0").lower() not in {"0", "false", "off", "no"}:
                    # O Analytics V7.8 não usa um endpoint inventado de itens do faturamento.
                    # Ele relaciona faturamento.orcamento_id -> pedido e usa os itens do pedido.
                    # Quando necessário, consultamos o endpoint individual OFICIAL do pedido.
                    enriched = []
                    total = len(records)
                    for idx, rec in enumerate(records, 1):
                        merged = dict(rec)
                        # Se o próprio retorno já contém itens, não há chamada extra.
                        if not _sales_items_for_storage(merged):
                            order_id = text(deep_find(rec, (
                                "orcamento_id", "id_orcamento", "pedido_id", "id_pedido",
                                "solicitacao_id", "id_solicitacao"
                            )))
                            if order_id:
                                try:
                                    detail = client.order_detail(order_id)
                                except Exception as e:
                                    detail = None
                                    with SessionLocal() as edb:
                                        erun = edb.get(SyncRun, run_id)
                                        erun.message = (erun.message or "") + f"\nPedido {order_id} do faturamento: detalhe não obtido ({e})"
                                        edb.commit()
                                if detail:
                                    # Mantém o cabeçalho da NF intacto e anexa o pedido como fonte de itens.
                                    merged["pedido_detalhe"] = detail
                        enriched.append(merged)
                        if idx == 1 or idx % 10 == 0 or idx == total:
                            _progress(f"Faturamento · vinculando itens dos pedidos {idx}/{total}")
                    records = enriched
            elif module == "receber":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records, cursor_meta = client.finance_period(
                        "receber", start_date, end_date, limit,
                        first_invalid=cursor.get("first_invalid") if cursor.get("initialized") else None,
                        page_hint=page_hint or None,
                    )
                else:
                    records = client.finance(
                        "receber", limit, None if recent else int(params.get("start_pos") or 1),
                        first_invalid=cursor.get("first_invalid") if cursor.get("initialized") else None,
                    )
            elif module == "pagar":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records, cursor_meta = client.finance_period(
                        "pagar", start_date, end_date, limit,
                        first_invalid=cursor.get("first_invalid") if cursor.get("initialized") else None,
                        page_hint=page_hint or None,
                    )
                else:
                    records = client.finance(
                        "pagar", limit, None if recent else int(params.get("start_pos") or 1),
                        first_invalid=cursor.get("first_invalid") if cursor.get("initialized") else None,
                    )
            elif module == "compras":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records, cursor_meta = client.purchases_period(
                        start_date, end_date, limit,
                        last_offset=cursor.get("next_offset") if cursor.get("initialized") else None,
                        page_hint=page_hint or None, force_cached_range=bool(page_hint) and (force_period or not page_hint.get("touches_recent")),
                    )
                else:
                    records = client.purchases(limit, None if recent else int(params.get("start_offset") or 0))
                if str(params.get("purchase_details") or "0").lower() not in {"0", "false", "off", "no"}:
                    enriched = []
                    total = len(records)
                    for idx, rec in enumerate(records, 1):
                        eid = external_id("compras", rec)
                        try:
                            detail = client.purchase_detail(eid)
                        except Exception as e:
                            detail = None
                            with SessionLocal() as db:
                                run = db.get(SyncRun, run_id)
                                run.message = (run.message or "") + f"\nCompra {eid}: detalhe não obtido ({e})"
                                db.commit()
                        if detail:
                            merged = dict(rec)
                            merged.update(detail)
                            # Mantém arrays de itens do detalhe, mesmo quando o resumo
                            # tinha campos homônimos vazios.
                            for key in ("produtos", "itens", "items", "produtos_itens"):
                                if key in detail:
                                    merged[key] = detail[key]
                            enriched.append(merged)
                        else:
                            enriched.append(rec)
                        if idx == 1 or idx % 10 == 0 or idx == total:
                            _progress(f"Compras · carregando detalhe {idx}/{total}")
                    records = enriched
            elif module == "despesas":
                if mode in {"period", "force_period"} and start_date and end_date:
                    records = client.expenses_period(start_date, end_date, max_days=max(limit, 366))
                else:
                    records = client.expenses(limit)
            else:
                raise ConnectorError(f"Módulo ERPFlex desconhecido: {module}")

        # Pedidos, faturamento e financeiro em modo período já são localizados e
        # filtrados pelo mesmo algoritmo do V7.8. Compras mantém o filtro local.
        if mode in {"period", "force_period"} and module in MOVEMENT_MODULES and module not in {"orders", "faturamento", "receber", "pagar", "compras", "despesas"}:
            before = len(records)
            records = _filter_period(module, records, start_date, end_date)
            with SessionLocal() as db:
                run = db.get(SyncRun, run_id)
                run.current_step = f"Período: {len(records)} de {before} registro(s) nos blocos consultados"
                db.commit()
        elif mode in {"period", "force_period"} and module in {"orders", "faturamento", "receber", "pagar", "compras"}:
            with SessionLocal() as db:
                run = db.get(SyncRun, run_id)
                if module == "faturamento":
                    run.current_step = f"Varredura regressiva concluída · {len(records)} faturamento(s) no período"
                elif module == "compras":
                    run.current_step = f"Compras do período localizadas · {len(records)} registro(s)"
                else:
                    run.current_step = f"Período localizado pelo motor V7.8 · {len(records)} registro(s)"
                if len(records) == 0 and module == "faturamento":
                    run.message = (run.message or "") + (
                        f"\nNenhum faturamento foi retornado entre {start_date} e {end_date}. "
                        "A sincronização varreu da última página para trás; confira a etapa exibida e aumente o limite se necessário."
                    )
                db.commit()

        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.2)
        _update_progress(run_id, percent=70.0, phase="Preparando gravação", current=0, total=len(records), detail=f"{len(records)} registro(s) recebidos da API")
        if not force_period:
            _save_sync_cursor(company_id, module, cursor_meta)
        elif cursor_meta:
            cursor_meta.setdefault("note", "Sincronização forçada: cursor incremental principal foi preservado.")
        _save_erp_records(run_id, module, records)
        if cursor_meta.get("note"):
            with SessionLocal() as db:
                run = db.get(SyncRun, run_id)
                if run:
                    run.message = ((run.message or "") + "\n" + str(cursor_meta.get("note"))).strip()
                    db.commit()
    except Exception as e:
        _update_progress(run_id, percent=100.0, phase="Falha", detail=str(e))
        with SessionLocal() as db:
            run = db.get(SyncRun, run_id)
            run.status = "ERRO"
            run.finished_at = datetime.utcnow()
            run.total_errors = max(1, int(run.total_errors or 0))
            run.message = str(e)
            run.current_step = "Falha"
            db.commit()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.2)


def _nf_meta_key(meta: dict) -> str | None:
    return access_key_from(meta)


def _nf_meta_nsu(meta: dict) -> str | None:
    v = deep_find(meta, ("nsu", "NSU", "numeroSequencial", "sequencial"))
    return str(v) if v not in (None, "") else None


def _run_nfstock(run_id: int, params: dict):
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        run.status = "EM_EXECUCAO"
        run.current_step = "Listando NF-e no NF Stock"
        db.commit()
    _update_progress(run_id, percent=5.0, phase="Consultando NF Stock", current=0, total=0, detail="Listando NF-e no período")
    heartbeat_stop, heartbeat_thread = _start_progress_heartbeat(run_id)
    try:
        start = str(params.get("start_date") or "")
        end = str(params.get("end_date") or "")
        if not start or not end:
            raise ConnectorError("Informe data inicial e final para o NF Stock.")
        with SessionLocal() as db:
            run = db.get(SyncRun, run_id)
            settings = get_nfstock_settings(db, run.company_id)
        client = NFStockClient(settings)
        metas = client.list_period(start, end, max_pages=max(1, min(500, int(params.get("max_pages") or 100))))
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.2)
        _update_progress(run_id, percent=20.0, phase="Processando NF-e", current=0, total=len(metas), detail=f"{len(metas)} documento(s) localizado(s)")
        with SessionLocal() as db:
            run = db.get(SyncRun, run_id)
            company_id = run.company_id
            run.total_found = len(metas)
            progress = db.get(SyncProgress, run_id)
            if not progress:
                progress = SyncProgress(run_id=run_id, percent=20.0, phase="Processando NF-e", current=0, total=len(metas), detail=f"0/{len(metas)}", heartbeat_at=datetime.utcnow(), stage_updated_at=datetime.utcnow())
                db.add(progress)
            db.commit()
            for idx, meta in enumerate(metas, 1):
                try:
                    key = _nf_meta_key(meta)
                    nsu = _nf_meta_nsu(meta)
                    full = None
                    if key:
                        full = client.document_by_key(key, xml=True)
                    elif nsu:
                        full = client.document_by_nsu(nsu, xml=True)
                    xml = client.xml_for(meta, full)
                    combined = dict(meta)
                    if isinstance(full, dict):
                        combined["detail"] = full
                    if xml:
                        combined["xml_available"] = True
                    raw, inserted = _raw_upsert(db, company_id, "NFSTOCK", "nfe", combined)
                    if not xml:
                        raise ConnectorError("NF-e sem XML disponível no retorno atual")
                    parsed = parse_nfe(xml, client.cnpj)
                    # NF Stock entra no núcleo de compras somente como ENTRADA ou INDEFINIDA.
                    if parsed.get("direction") == "SAIDA":
                        run.total_processed = idx
                        run.current_step = f"Ignorando saída {idx}/{len(metas)}"
                        continue
                    key = parsed.get("access_key") or key
                    row = db.scalar(select(PurchaseInvoice).where(
                        PurchaseInvoice.company_id == company_id,
                        PurchaseInvoice.access_key == key
                    ).limit(1)) if key else None
                    supplier = _partner(db, company_id, "supplier", meta, nf_xml=parsed)
                    # Se a compra do ERPFlex chegou primeiro sem chave, tenta vínculo conservador
                    # por número + data + valor. Só vincula quando há exatamente um candidato.
                    if not row and parsed.get("number"):
                        candidates = db.scalars(select(PurchaseInvoice).where(
                            PurchaseInvoice.company_id == company_id,
                            PurchaseInvoice.access_key.is_(None),
                            PurchaseInvoice.erpflex_id.is_not(None),
                            PurchaseInvoice.number == str(parsed.get("number")),
                        )).all()
                        issue = normalize_date(parsed.get("issue_date"))
                        total_nf = fnum(parsed.get("total"))
                        matches=[]
                        for cand in candidates:
                            date_ok = (not issue or not cand.issue_date or cand.issue_date == issue)
                            value_ok = (not total_nf or not cand.total_erpflex or abs(cand.total_erpflex-total_nf) <= 0.01)
                            if date_ok and value_ok:
                                matches.append(cand)
                        if len(matches) == 1:
                            row = matches[0]
                            row.access_key = key
                    if not row:
                        row = PurchaseInvoice(company_id=company_id, access_key=key)
                        db.add(row)
                        db.flush()
                    row.nfstock_nsu = nsu or row.nfstock_nsu
                    row.supplier_id = supplier.id if supplier else row.supplier_id
                    row.number = parsed.get("number") or row.number
                    row.series = parsed.get("series") or row.series
                    row.issue_date = normalize_date(parsed.get("issue_date")) or row.issue_date
                    row.total_nfstock = fnum(parsed.get("total")) or row.total_nfstock
                    row.xml_text = xml
                    row.nfstock_raw_id = raw.id
                    _reconcile(row)
                    db.flush()
                    # XML é a fonte dos itens de NF-e de entrada; substitui apenas os itens dessa nota.
                    db.query(PurchaseItem).filter(PurchaseItem.purchase_invoice_id == row.id).delete(synchronize_session=False)
                    for item in parsed.get("items") or []:
                        db.add(PurchaseItem(
                            purchase_invoice_id=row.id,
                            product_code=text(item.get("product_code")) or None,
                            ean=text(item.get("ean")) or None,
                            description=text(item.get("description")),
                            ncm=text(item.get("ncm")) or None,
                            cfop=text(item.get("cfop")) or None,
                            unit=text(item.get("unit")) or None,
                            qty=fnum(item.get("qty")), unit_price=fnum(item.get("unit_price")), total=fnum(item.get("total")),
                            icms=fnum(item.get("icms")), icms_st=fnum(item.get("icms_st")), ipi=fnum(item.get("ipi")),
                            pis=fnum(item.get("pis")), cofins=fnum(item.get("cofins")),
                        ))
                    run.total_inserted += 1 if inserted else 0
                    run.total_updated += 0 if inserted else 1
                    run.total_processed = idx
                    run.current_step = f"Processando NF-e {idx}/{len(metas)}"
                    if progress:
                        progress.percent = 20.0 + 78.0 * idx / max(1, len(metas))
                        progress.phase = "Processando NF-e"
                        progress.current = idx
                        progress.total = len(metas)
                        progress.detail = run.current_step
                        progress.heartbeat_at = datetime.utcnow()
                        progress.stage_updated_at = datetime.utcnow()
                    if idx % 5 == 0:
                        db.commit()
                except Exception as e:
                    run.total_errors += 1
                    run.total_processed = idx
                    run.message = (run.message or "") + f"\nNF {idx}: {e}"
                    if idx % 5 == 0:
                        db.commit()
            run.status = "CONCLUIDO" if run.total_errors == 0 else "CONCLUIDO_COM_ERROS"
            run.finished_at = datetime.utcnow()
            run.current_step = "Concluído"
            if progress:
                progress.percent = 100.0
                progress.phase = "Concluído"
                progress.current = len(metas)
                progress.total = len(metas)
                progress.detail = f"{len(metas)} documento(s) processado(s)"
                progress.heartbeat_at = datetime.utcnow()
                progress.stage_updated_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        _update_progress(run_id, percent=100.0, phase="Falha", detail=str(e))
        with SessionLocal() as db:
            run = db.get(SyncRun, run_id)
            run.status = "ERRO"
            run.finished_at = datetime.utcnow()
            run.total_errors = max(1, int(run.total_errors or 0))
            run.message = str(e)
            run.current_step = "Falha"
            db.commit()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=0.2)


def recover_interrupted_sync_runs(stale_seconds: int = 120) -> int:
    """Marca apenas execuções realmente órfãs/interrompidas.

    Desde a V1.3.30 o worker pode rodar em outro processo/serviço Railway. Um
    reinício do servidor Web não significa que a sincronização morreu. Por isso
    preservamos runs com heartbeat recente e só encerramos os que ficaram sem
    sinal além da tolerância.
    """
    count = 0
    cutoff = datetime.utcnow() - timedelta(seconds=max(30, int(stale_seconds)))
    with SessionLocal() as db:
        rows = db.scalars(select(SyncRun).where(SyncRun.status.in_(["PENDENTE", "EM_EXECUCAO"]))).all()
        for r in rows:
            progress = db.get(SyncProgress, r.id)
            heartbeat = progress.heartbeat_at if progress else None
            if heartbeat and heartbeat >= cutoff:
                continue
            # PENDENTE muito recente pode ainda estar aguardando a thread/worker iniciar.
            if not heartbeat and r.started_at and r.started_at >= cutoff:
                continue
            r.status = "INTERROMPIDO"
            r.finished_at = datetime.utcnow()
            r.total_errors = max(1, int(r.total_errors or 0))
            r.current_step = "Interrompido: heartbeat do processo expirou"
            r.message = (r.message or "") + ("\n" if r.message else "") + "A execução ficou sem heartbeat e foi considerada órfã após reinício/falha do processo."
            if progress:
                progress.percent = 100.0
                progress.phase = "Interrompido"
                progress.detail = r.current_step
                progress.heartbeat_at = datetime.utcnow()
                progress.stage_updated_at = datetime.utcnow()
            count += 1
        if count:
            db.commit()
    return count


def start_sync_batch(company_id: int, modules: list[str], params: dict) -> list[int]:
    modules = [m for m in modules if m in MASTER_MODULES or m in MOVEMENT_MODULES]
    if not modules:
        return []
    run_ids: list[int] = []
    with SessionLocal() as db:
        for module in modules:
            run = SyncRun(company_id=company_id, source="ERPFLEX", module=module, status="PENDENTE", params_json=safe_json(params))
            db.add(run)
            db.flush()
            run_ids.append(run.id)
        db.commit()

    def worker():
        owner = f"batch:{run_ids[0]}:{uuid.uuid4().hex}"
        if not _sync_lock.acquire(blocking=False):
            with SessionLocal() as db:
                for rid in run_ids:
                    r = db.get(SyncRun, rid)
                    if r:
                        r.status = "ERRO"
                        r.finished_at = datetime.utcnow()
                        r.message = "Já existe uma sincronização em execução neste processo. Aguarde a conclusão."
                db.commit()
            return
        lease_ok = False
        try:
            lease_ok = _acquire_execution_lease(company_id, "ERPFLEX", owner)
            if not lease_ok:
                with SessionLocal() as db:
                    for rid in run_ids:
                        r = db.get(SyncRun, rid)
                        if r:
                            r.status = "ERRO"
                            r.finished_at = datetime.utcnow()
                            r.message = "Já existe uma sincronização ERPFlex ativa em outro processo/worker. Aguarde a conclusão."
                    db.commit()
                return
            for rid, module in zip(run_ids, modules):
                _run_erp(rid, module, params)
        finally:
            if lease_ok:
                _release_execution_lease(company_id, "ERPFLEX", owner)
            _sync_lock.release()

    threading.Thread(target=worker, name=f"sync-ERPFLEX-batch-{run_ids[0]}", daemon=True).start()
    return run_ids


def start_sync(company_id: int, source: str, module: str, params: dict) -> int:
    with SessionLocal() as db:
        run = SyncRun(company_id=company_id, source=source, module=module, status="PENDENTE", params_json=safe_json(params))
        db.add(run)
        db.commit()
        run_id = run.id

    def worker():
        owner = f"single:{run_id}:{uuid.uuid4().hex}"
        if not _sync_lock.acquire(blocking=False):
            with SessionLocal() as db:
                r = db.get(SyncRun, run_id)
                r.status = "ERRO"
                r.finished_at = datetime.utcnow()
                r.message = "Já existe uma sincronização em execução neste processo. Aguarde a conclusão."
                db.commit()
            return
        lease_ok = False
        try:
            lease_ok = _acquire_execution_lease(company_id, source, owner)
            if not lease_ok:
                with SessionLocal() as db:
                    r = db.get(SyncRun, run_id)
                    r.status = "ERRO"
                    r.finished_at = datetime.utcnow()
                    r.message = f"Já existe uma sincronização {source} ativa em outro processo/worker. Aguarde a conclusão."
                    db.commit()
                return
            if source == "ERPFLEX":
                _run_erp(run_id, module, params)
            elif source == "NFSTOCK":
                _run_nfstock(run_id, params)
            else:
                raise ConnectorError(f"Fonte desconhecida: {source}")
        finally:
            if lease_ok:
                _release_execution_lease(company_id, source, owner)
            _sync_lock.release()

    threading.Thread(target=worker, name=f"sync-{source}-{module}-{run_id}", daemon=True).start()
    return run_id

