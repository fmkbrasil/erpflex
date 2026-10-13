from __future__ import annotations

import os
import socket
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import (
    Company,
    SessionLocal,
    SyncAutomationConfig,
    SyncAutomationModule,
    SyncRun,
    SyncWorkerHeartbeat,
    init_db,
)
from .sync_service import MASTER_MODULES, MOVEMENT_MODULES, recover_interrupted_sync_runs, start_sync_batch

WORKER_NAME = os.getenv("SYNC_WORKER_NAME") or "sync-worker"
INSTANCE_ID = os.getenv("RAILWAY_REPLICA_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
APP_TIMEZONE = os.getenv("APP_TIMEZONE") or "America/Sao_Paulo"
TERMINAL = {"CONCLUIDO", "CONCLUIDO_COM_ERROS", "ERRO", "INTERROMPIDO"}
DEFAULT_INTERVALS = {
    "products": 360,
    "banks": 720,
    "clientes": 120,
    "orders": 15,
    "faturamento": 15,
    "receber": 15,
    "pagar": 30,
    "compras": 30,
    "despesas": 60,
}


def _ensure_defaults(company_id: int) -> None:
    with SessionLocal() as db:
        cfg = db.scalar(select(SyncAutomationConfig).where(SyncAutomationConfig.company_id == company_id).limit(1))
        if not cfg:
            # Segurança: o worker é instalado pronto, porém começa pausado até o usuário habilitar.
            cfg = SyncAutomationConfig(company_id=company_id, active=False, poll_seconds=30)
            db.add(cfg)
        for module, minutes in DEFAULT_INTERVALS.items():
            row = db.scalar(select(SyncAutomationModule).where(
                SyncAutomationModule.company_id == company_id,
                SyncAutomationModule.module == module,
            ).limit(1))
            if not row:
                db.add(SyncAutomationModule(
                    company_id=company_id,
                    module=module,
                    enabled=module in {"orders", "faturamento", "receber"},
                    interval_minutes=minutes,
                ))
        db.commit()


def _heartbeat(company_id: int, status: str, module: str | None = None, run_id: int | None = None, message: str | None = None) -> None:
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.scalar(select(SyncWorkerHeartbeat).where(
            SyncWorkerHeartbeat.company_id == company_id,
            SyncWorkerHeartbeat.worker_name == WORKER_NAME,
            SyncWorkerHeartbeat.instance_id == INSTANCE_ID,
        ).limit(1))
        if not row:
            row = SyncWorkerHeartbeat(
                company_id=company_id,
                worker_name=WORKER_NAME,
                instance_id=INSTANCE_ID,
                started_at=now,
            )
            db.add(row)
        row.status = status
        row.current_module = module
        row.current_run_id = run_id
        row.message = (message or "")[:500] or None
        row.heartbeat_at = now
        db.commit()


def _within_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    h = now.hour
    start_hour = max(0, min(23, int(start_hour)))
    end_hour = max(0, min(23, int(end_hour)))
    if start_hour <= end_hour:
        return start_hour <= h <= end_hour
    # janela que cruza meia-noite, ex. 22 -> 05
    return h >= start_hour or h <= end_hour


def _due_modules(company_id: int, now: datetime) -> list[SyncAutomationModule]:
    with SessionLocal() as db:
        rows = db.scalars(select(SyncAutomationModule).where(
            SyncAutomationModule.company_id == company_id,
            SyncAutomationModule.enabled.is_(True),
        ).order_by(SyncAutomationModule.id.asc())).all()
        return [r for r in rows if not r.next_run_at or r.next_run_at <= now]


def _mark_started(company_id: int, modules: list[str], run_ids: list[int], now: datetime) -> None:
    by_module = dict(zip(modules, run_ids))
    with SessionLocal() as db:
        for module in modules:
            row = db.scalar(select(SyncAutomationModule).where(
                SyncAutomationModule.company_id == company_id,
                SyncAutomationModule.module == module,
            ).limit(1))
            if row:
                row.last_started_at = now
                row.last_run_id = by_module.get(module)
                row.last_status = "PENDENTE"
        db.commit()


def _mark_finished(company_id: int, modules: list[str], statuses: dict[str, str], now: datetime) -> None:
    with SessionLocal() as db:
        for module in modules:
            row = db.scalar(select(SyncAutomationModule).where(
                SyncAutomationModule.company_id == company_id,
                SyncAutomationModule.module == module,
            ).limit(1))
            if row:
                row.last_finished_at = now
                row.last_status = statuses.get(module) or row.last_status
                row.next_run_at = now + timedelta(minutes=max(1, int(row.interval_minutes or 30)))
        db.commit()


def _wait_runs(company_id: int, modules: list[str], run_ids: list[int]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    pending = set(run_ids)
    by_id = dict(zip(run_ids, modules))
    while pending:
        with SessionLocal() as db:
            rows = db.scalars(select(SyncRun).where(SyncRun.id.in_(list(pending)))).all()
            for run in rows:
                _heartbeat(company_id, "EXECUTANDO", run.module, run.id, run.current_step or run.status)
                if run.status in TERMINAL:
                    statuses[run.module] = run.status
                    pending.discard(run.id)
        if pending:
            time.sleep(2)
    return statuses


def run_forever() -> None:
    init_db()
    recover_interrupted_sync_runs()
    print(f"[worker] {WORKER_NAME} instance={INSTANCE_ID} iniciado", flush=True)
    while True:
        with SessionLocal() as db:
            companies = db.scalars(select(Company).where(Company.active.is_(True)).order_by(Company.id.asc())).all()
            company_ids = [c.id for c in companies]
        sleep_for = 30
        for company_id in company_ids:
            _ensure_defaults(company_id)
            with SessionLocal() as db:
                cfg = db.scalar(select(SyncAutomationConfig).where(SyncAutomationConfig.company_id == company_id).limit(1))
                if not cfg:
                    continue
                sleep_for = min(sleep_for, max(5, min(300, int(cfg.poll_seconds or 30))))
                active = bool(cfg.active)
                params = {
                    "mode": "recent",
                    "max_blocks": max(1, min(1000, int(cfg.max_blocks or 100))),
                    "master_max_pages": max(1, min(2000, int(cfg.master_max_pages or 500))),
                    "max_clients": max(1, min(5000, int(cfg.max_clients or 1000))),
                    "sales_details": "1" if cfg.sales_details else "0",
                    "purchase_details": "1" if cfg.purchase_details else "0",
                    "automation": True,
                    "worker_instance": INSTANCE_ID,
                }
                start_hour = cfg.allowed_start_hour
                end_hour = cfg.allowed_end_hour
            now = datetime.utcnow()
            try:
                local_now = datetime.now(ZoneInfo(APP_TIMEZONE))
            except Exception:
                local_now = datetime.utcnow()
            if not active:
                _heartbeat(company_id, "PAUSADO", message="Automação desativada na Central de Sincronização")
                continue
            if not _within_window(local_now, start_hour, end_hour):
                _heartbeat(company_id, "AGUARDANDO", message=f"Fora da janela {start_hour:02d}:00–{end_hour:02d}:59 ({APP_TIMEZONE})")
                continue
            due = _due_modules(company_id, now)
            modules = [r.module for r in due if r.module in MASTER_MODULES or r.module in MOVEMENT_MODULES]
            if not modules:
                _heartbeat(company_id, "ONLINE", message="Aguardando próxima execução")
                continue
            _heartbeat(company_id, "INICIANDO", message="Módulos: " + ", ".join(modules))
            run_ids = start_sync_batch(company_id, modules, params)
            if not run_ids:
                _heartbeat(company_id, "ERRO", message="Não foi possível criar as execuções")
                continue
            _mark_started(company_id, modules, run_ids, now)
            statuses = _wait_runs(company_id, modules, run_ids)
            _mark_finished(company_id, modules, statuses, datetime.utcnow())
            _heartbeat(company_id, "ONLINE", message="Lote concluído: " + ", ".join(f"{m}={statuses.get(m, '?')}" for m in modules))
        time.sleep(max(5, sleep_for))


if __name__ == "__main__":
    run_forever()
