from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    create_engine, select, inspect, text as sql_text
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import BACKUP_DIR, DATA_DIR, migrate_legacy_sqlite_if_needed

migrate_legacy_sqlite_if_needed()


def _database_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if raw:
        if raw.startswith("postgres://"):
            raw = "postgresql+psycopg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql://"):
            raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
        return raw
    return f"sqlite:///{(DATA_DIR / 'plataforma_integrada.db').as_posix()}"

DATABASE_URL = _database_url()
ENGINE_KW = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    ENGINE_KW["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **ENGINE_KW)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    pass




SCHEMA_VERSION = 10


class SchemaState(Base):
    __tablename__ = "schema_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_backup: Mapped[str | None] = mapped_column(String(500), nullable=True)


class IntegrationConfig(Base):
    __tablename__ = "integration_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    crm: Mapped[str | None] = mapped_column(String(30), nullable=True)
    company_cnpj: Mapped[str | None] = mapped_column(String(20), nullable=True)
    page_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "provider", name="uq_integration_company_provider"),)


class EmailConfig(Base):
    __tablename__ = "email_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, default=587)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    security: Mapped[str] = mapped_column(String(20), default="starttls")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CollectionAutomationConfig(Base):
    __tablename__ = "collection_automation_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    days_before_due: Mapped[int] = mapped_column(Integer, default=3)
    include_overdue: Mapped[bool] = mapped_column(Boolean, default=True)
    resend_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    subject_template: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CollectionCustomerRule(Base):
    __tablename__ = "collection_customer_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), index=True)
    has_collection_contract: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "partner_id", name="uq_collection_customer_rule"),)


class CollectionSendLog(Base):
    __tablename__ = "collection_send_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    financial_title_id: Mapped[int] = mapped_column(ForeignKey("financial_titles.id", ondelete="CASCADE"), index=True)
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(30), default="AUTO", index=True)
    status: Mapped[str] = mapped_column(String(30), default="ENVIADO", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), default="Empresa")
    trade_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    cnpj: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)




class Branch(Base):
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(180), default="Matriz")
    code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cnpj: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_branch_company_name"),)


class AccessProfile(Base):
    __tablename__ = "access_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    system: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProfilePermission(Base):
    __tablename__ = "profile_permissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("access_profiles.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(120), index=True)
    __table_args__ = (UniqueConstraint("profile_id", "permission", name="uq_profile_permission"),)


class AppUser(Base):
    __tablename__ = "app_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180), default="")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserCompanyAccess(Base):
    __tablename__ = "user_company_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("access_profiles.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    __table_args__ = (UniqueConstraint("user_id", "company_id", name="uq_user_company"),)


class UserBranchAccess(Base):
    __tablename__ = "user_branch_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    __table_args__ = (UniqueConstraint("user_id", "branch_id", name="uq_user_branch"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity: Mapped[str | None] = mapped_column(String(120), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class UserViewPreference(Base):
    __tablename__ = "user_view_preferences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    view_key: Mapped[str] = mapped_column(String(120), index=True)
    columns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    per_page: Mapped[int] = mapped_column(Integer, default=50)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "company_id", "view_key", name="uq_user_view_preference"),)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDENTE", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_found: Mapped[int] = mapped_column(Integer, default=0)
    total_processed: Mapped[int] = mapped_column(Integer, default=0)
    total_inserted: Mapped[int] = mapped_column(Integer, default=0)
    total_updated: Mapped[int] = mapped_column(Integer, default=0)
    total_errors: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncProgress(Base):
    """Progresso vivo de uma sincronização.

    Fica em tabela separada para evoluir sem alterar o histórico SyncRun e para
    permitir heartbeat enquanto uma chamada remota está aguardando resposta.
    """
    __tablename__ = "sync_progress"
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True)
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    phase: Mapped[str | None] = mapped_column(String(120), nullable=True)
    current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    stage_updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncCursor(Base):
    """Estado persistente de navegação da API ERPFlex por empresa/módulo.

    Replica o conceito de ModuleState usado no ERPFlex Analytics V7.8 para que
    a plataforma não precise redescobrir toda a borda da API a cada execução.
    """
    __tablename__ = "sync_cursors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="ERPFLEX", index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    next_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_valid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_invalid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coverage_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coverage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    last_http_requests: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "source", "module", name="uq_sync_cursor_company_source_module"),)


class SyncPageIndex(Base):
    """Índice local das páginas/offsets já observados na API ERPFlex.

    Guarda apenas metadados da navegação. Os documentos continuam persistidos nas
    tabelas centrais/RawRecord; o índice serve para localizar períodos sem
    redescobrir toda a paginação.
    """
    __tablename__ = "sync_page_index"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="ERPFLEX", index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    cursor_kind: Mapped[str] = mapped_column(String(24), index=True)
    cursor_value: Mapped[int] = mapped_column(Integer, index=True)
    min_date: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    max_date: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    first_external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("company_id", "source", "module", "cursor_kind", "cursor_value", name="uq_sync_page_index_location"),
        Index("ix_sync_page_index_period", "company_id", "module", "min_date", "max_date"),
    )


class SyncAutomationConfig(Base):
    """Configuração do worker de sincronização automática por empresa."""
    __tablename__ = "sync_automation_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    poll_seconds: Mapped[int] = mapped_column(Integer, default=30)
    allowed_start_hour: Mapped[int] = mapped_column(Integer, default=0)
    allowed_end_hour: Mapped[int] = mapped_column(Integer, default=23)
    max_blocks: Mapped[int] = mapped_column(Integer, default=100)
    master_max_pages: Mapped[int] = mapped_column(Integer, default=500)
    max_clients: Mapped[int] = mapped_column(Integer, default=1000)
    sales_details: Mapped[bool] = mapped_column(Boolean, default=True)
    purchase_details: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncAutomationModule(Base):
    """Agenda incremental por módulo. Cada módulo pode ter sua própria frequência."""
    __tablename__ = "sync_automation_modules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "module", name="uq_sync_automation_company_module"),)


class SyncWorkerHeartbeat(Base):
    """Heartbeat do processo worker separado do servidor web."""
    __tablename__ = "sync_worker_heartbeats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    worker_name: Mapped[str] = mapped_column(String(120), default="sync-worker", index=True)
    instance_id: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(40), default="ONLINE", index=True)
    current_module: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (UniqueConstraint("company_id", "worker_name", "instance_id", name="uq_sync_worker_instance"),)


class SyncExecutionLease(Base):
    """Lease no banco para impedir sincronização ERPFlex simultânea entre Web e Worker."""
    __tablename__ = "sync_execution_leases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="ERPFLEX", index=True)
    owner: Mapped[str] = mapped_column(String(160), index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    __table_args__ = (UniqueConstraint("company_id", "source", name="uq_sync_execution_lease"),)


class RawRecord(Base):
    __tablename__ = "raw_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(160))
    record_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "source", "module", "external_id", name="uq_raw_origin"),)


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    cnpj_cpf: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    trade_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    role_supplier: Mapped[bool] = mapped_column(Boolean, default=False)
    erpflex_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("ix_partner_company_cnpj", "company_id", "cnpj_cpf"),
    )


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    erpflex_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    ean: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    ncm: Mapped[str | None] = mapped_column(String(30), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    category: Mapped[str | None] = mapped_column(String(160), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("company_id", "erpflex_id", name="uq_product_erpflex"),
        Index("ix_product_company_code", "company_id", "code"),
    )


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    erpflex_id: Mapped[str] = mapped_column(String(120))
    number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    issue_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    forecast_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0)
    raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "erpflex_id", name="uq_sales_order_erp"),)


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    erpflex_id: Mapped[str] = mapped_column(String(120))
    access_key: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    series: Mapped[str | None] = mapped_column(String(30), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    issue_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    forecast_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    total: Mapped[float] = mapped_column(Float, default=0)
    carrier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    carrier2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    delivery_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    delivery_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    delivery_zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    volumes: Mapped[float] = mapped_column(Float, default=0)
    raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("company_id", "erpflex_id", name="uq_sales_invoice_erp"),
        Index("ix_sales_invoice_company_key", "company_id", "access_key"),
    )


class SalesInvoiceItem(Base):
    __tablename__ = "sales_invoice_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_invoice_id: Mapped[int] = mapped_column(ForeignKey("sales_invoices.id", ondelete="CASCADE"), index=True)
    erpflex_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    ncm: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cfop: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    icms: Mapped[float] = mapped_column(Float, default=0)
    ipi: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (Index("ix_sales_invoice_item_invoice_product", "sales_invoice_id", "product_id"),)


class PurchaseInvoice(Base):
    __tablename__ = "purchase_invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    access_key: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    erpflex_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    nfstock_nsu: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    series: Mapped[str | None] = mapped_column(String(30), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    issue_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    total_erpflex: Mapped[float] = mapped_column(Float, default=0)
    total_nfstock: Mapped[float] = mapped_column(Float, default=0)
    reconciliation_status: Mapped[str] = mapped_column(String(40), default="PENDENTE", index=True)
    xml_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    erpflex_raw_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"), nullable=True)
    nfstock_raw_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("ix_purchase_company_key", "company_id", "access_key"),
        UniqueConstraint("company_id", "erpflex_id", name="uq_purchase_erp"),
    )


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_invoice_id: Mapped[int] = mapped_column(ForeignKey("purchase_invoices.id", ondelete="CASCADE"), index=True)
    product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    ncm: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cfop: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    icms: Mapped[float] = mapped_column(Float, default=0)
    icms_st: Mapped[float] = mapped_column(Float, default=0)
    ipi: Mapped[float] = mapped_column(Float, default=0)
    pis: Mapped[float] = mapped_column(Float, default=0)
    cofins: Mapped[float] = mapped_column(Float, default=0)


class FinancialTitle(Base):
    __tablename__ = "financial_titles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # RECEBER/PAGAR/DESPESA
    erpflex_id: Mapped[str] = mapped_column(String(120))
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    document: Mapped[str | None] = mapped_column(String(100), nullable=True)
    issue_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    paid_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    value: Mapped[float] = mapped_column(Float, default=0)
    paid_value: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    bank: Mapped[str | None] = mapped_column(String(120), nullable=True)
    wallet: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "kind", "erpflex_id", name="uq_financial_origin"),)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    sales_invoice_id: Mapped[int] = mapped_column(ForeignKey("sales_invoices.id"), unique=True, index=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    responsible: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    responsible_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    volumes: Mapped[float] = mapped_column(Float, default=0)
    box_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDENTE", index=True)
    km: Mapped[float] = mapped_column(Float, default=0)
    freight_cost: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Responsible(Base):
    __tablename__ = "responsibles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(60), default="Motorista Interno", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    default_freight_type: Mapped[str] = mapped_column(String(60), default="Valor fechado")
    default_freight_value: Mapped[float] = mapped_column(Float, default=0)
    default_freight_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "name", "kind", name="uq_responsible_company_name_kind"),)


class Region(Base):
    __tablename__ = "regions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_region_company_name"),)


class BoxType(Base):
    __tablename__ = "box_types"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_box_company_name"),)


class DeliveryAssignment(Base):
    __tablename__ = "delivery_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), unique=True, index=True)
    responsible_id: Mapped[int] = mapped_column(ForeignKey("responsibles.id"), index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeliveryRule(Base):
    __tablename__ = "delivery_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_cnpj: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    responsible_id: Mapped[int | None] = mapped_column(ForeignKey("responsibles.id"), nullable=True, index=True)
    box_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    priority: Mapped[str] = mapped_column(String(30), default="Normal")
    window_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    window_end: Mapped[str | None] = mapped_column(String(10), nullable=True)
    stop_minutes: Mapped[int] = mapped_column(Integer, default=10)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("company_id", "customer_cnpj", "customer_name", name="uq_delivery_rule_customer"),)


class Route(Base):
    __tablename__ = "routes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    responsible_id: Mapped[int] = mapped_column(ForeignKey("responsibles.id"), index=True)
    route_date: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(40), default="PLANEJADO", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (Index("ix_route_company_date_resp", "company_id", "route_date", "responsible_id"),)


class RouteStop(Base):
    __tablename__ = "route_stops"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("routes.id", ondelete="CASCADE"), index=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    stop_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("route_id", "delivery_id", name="uq_route_stop_delivery"),)


class DeliveryMovement(Base):
    __tablename__ = "delivery_movements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    movement_type: Mapped[str] = mapped_column(String(50), index=True)
    old_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class DeliveryOccurrence(Base):
    __tablename__ = "delivery_occurrences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    responsible_id: Mapped[int | None] = mapped_column(ForeignKey("responsibles.id"), nullable=True, index=True)
    occurrence_type: Mapped[str] = mapped_column(String(120), default="Ocorrência")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RouteCost(Base):
    __tablename__ = "route_costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("routes.id", ondelete="CASCADE"), unique=True, index=True)
    charge_type: Mapped[str] = mapped_column(String(60), default="Valor fechado")
    freight_value: Mapped[float] = mapped_column(Float, default=0)
    freight_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_initial: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_final: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_realized: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class LogisticsImport(Base):
    __tablename__ = "logistics_imports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="CONCLUIDO", index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    total_invoices: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    ignored: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (Index("ix_logistics_import_company_date", "company_id", "imported_at"),)


DEFAULT_PERMISSIONS = {
    "Administrador": ["*"],
    "Diretoria": [
        "dashboard.view", "cadastros.view", "faturamento.view", "financeiro.view", "compras.view",
        "logistica.view", "roteirizar.view", "painel_entregas.view", "painel_gerencial.view", "relatorios.view", "roteiros.view", "ocorrencias.view", "diagnostico.view", "api.read"
    ],
    "Operacional": [
        "dashboard.view", "logistica.view", "logistica.edit", "roteirizar.view", "roteirizar.manage", "importar.manage",
        "painel_entregas.view", "painel_gerencial.view", "relatorios.view", "roteiros.view", "roteiros.manage",
        "baixa.manage", "ocorrencias.view", "ocorrencias.manage", "responsaveis.view", "responsaveis.manage",
        "regras.manage", "parametros.manage", "api.read"
    ],
}


def _seed_security(db, company: Company):
    from .security import hash_password
    profiles = {}
    for name, permissions in DEFAULT_PERMISSIONS.items():
        profile = db.scalar(select(AccessProfile).where(AccessProfile.name == name).limit(1))
        if not profile:
            profile = AccessProfile(name=name, system=True, description=f"Perfil padrão {name}")
            db.add(profile); db.flush()
        profiles[name] = profile
        existing = set(db.scalars(select(ProfilePermission.permission).where(ProfilePermission.profile_id == profile.id)).all())
        for perm in permissions:
            if perm not in existing:
                db.add(ProfilePermission(profile_id=profile.id, permission=perm))
    branch = db.scalar(select(Branch).where(Branch.company_id == company.id).order_by(Branch.id).limit(1))
    if not branch:
        branch = Branch(company_id=company.id, name="Matriz", code="MATRIZ", cnpj=company.cnpj)
        db.add(branch); db.flush()
    app_user = (os.getenv("APP_USER") or "admin").strip()
    app_password = os.getenv("APP_PASSWORD") or ""
    if app_password:
        user = db.scalar(select(AppUser).where(AppUser.username == app_user).limit(1))
        if not user:
            user = AppUser(username=app_user, name="Administrador", password_hash=hash_password(app_password), is_superuser=True)
            db.add(user); db.flush()
        access = db.scalar(select(UserCompanyAccess).where(UserCompanyAccess.user_id == user.id, UserCompanyAccess.company_id == company.id).limit(1))
        if not access:
            db.add(UserCompanyAccess(user_id=user.id, company_id=company.id, profile_id=profiles["Administrador"].id))
        baccess = db.scalar(select(UserBranchAccess).where(UserBranchAccess.user_id == user.id, UserBranchAccess.branch_id == branch.id).limit(1))
        if not baccess:
            db.add(UserBranchAccess(user_id=user.id, branch_id=branch.id))


def _schema_version_from_db() -> int:
    try:
        inspector = inspect(engine)
        if "schema_state" not in inspector.get_table_names():
            return 0
        with engine.connect() as conn:
            value = conn.execute(sql_text("SELECT current_version FROM schema_state WHERE id=1")).scalar()
            return int(value or 0)
    except Exception:
        return 0


def _sqlite_db_path() -> Path | None:
    if not DATABASE_URL.startswith("sqlite"):
        return None
    try:
        raw = make_url(DATABASE_URL).database
        return Path(raw).expanduser() if raw else None
    except Exception:
        return None


def create_sqlite_backup(reason: str = "manual") -> Path | None:
    path = _sqlite_db_path()
    if not path or not path.exists() or path.stat().st_size <= 0:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:40]
    dest = BACKUP_DIR / f"plataforma_integrada_{stamp}_{safe_reason}.db"
    shutil.copy2(path, dest)
    # Em instalações locais, a chave de criptografia das integrações também é persistente.
    # Mantemos uma cópia junto aos backups para recuperação/migração de máquina.
    integration_key = DATA_DIR / "integration.key"
    if integration_key.exists():
        try:
            shutil.copy2(integration_key, BACKUP_DIR / "integration.key")
        except Exception:
            pass
    return dest


def database_status() -> dict:
    kind = "SQLite" if DATABASE_URL.startswith("sqlite") else ("PostgreSQL" if DATABASE_URL.startswith("postgresql") else "Outro")
    try:
        display_url = make_url(DATABASE_URL).render_as_string(hide_password=True)
    except Exception:
        display_url = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    path = _sqlite_db_path()
    backups = sorted(BACKUP_DIR.glob("plataforma_integrada_*.db"), reverse=True) if BACKUP_DIR.exists() else []
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID"))
    persistence_warning = None
    if kind == "SQLite" and on_railway and not (os.getenv("DATA_DIR") or "").strip():
        persistence_warning = "Railway detectado com SQLite sem DATA_DIR/Volume persistente. Configure PostgreSQL ou monte um Volume e defina DATA_DIR=/data antes de usar dados reais."
    return {
        "kind": kind,
        "url": display_url,
        "data_dir": str(DATA_DIR),
        "schema_version": _schema_version_from_db(),
        "target_schema_version": SCHEMA_VERSION,
        "sqlite_path": str(path) if path else None,
        "sqlite_size": path.stat().st_size if path and path.exists() else None,
        "last_backup": str(backups[0]) if backups else None,
        "backup_count": len(backups),
        "persistence_warning": persistence_warning,
        "persistent_recommended": not bool(persistence_warning),
    }


def _apply_schema_migrations(previous_version: int, backup_path: Path | None) -> None:
    # V1: estrutura-base. V2: integrações seguras. V3: preferências de visualização. V4: cursores persistentes do motor ERPFlex V7.8.
    # V5: progresso/heartbeat de sincronização em tabela separada.
    # V6: itens normalizados de faturamento/NF-e de saída.
    # V7: configuração SMTP persistente para reenvio de boleto; somente nova tabela.
    # V8: parâmetros de cobrança automática, clientes com contrato de cobrança e histórico de envios.
    # V9: índice persistente de páginas/offsets ERPFlex para sincronização incremental e forçada por período.
    # V10: worker automático, agenda por módulo, heartbeat e lease de execução entre processos.
    # create_all cria novas tabelas sem apagar ou recriar tabelas existentes.
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        state = db.get(SchemaState, 1)
        if not state:
            state = SchemaState(id=1, current_version=previous_version)
            db.add(state)
        if backup_path:
            state.last_backup = str(backup_path)
        state.current_version = SCHEMA_VERSION
        state.updated_at = datetime.utcnow()
        db.commit()


def init_db() -> None:
    previous_version = _schema_version_from_db()
    backup_path = None
    if previous_version < SCHEMA_VERSION:
        backup_path = create_sqlite_backup(f"antes_schema_{previous_version}_para_{SCHEMA_VERSION}")
    _apply_schema_migrations(previous_version, backup_path)
    with SessionLocal() as db:
        company = db.scalar(select(Company).limit(1))
        if not company:
            cnpj = "".join(ch for ch in (os.getenv("COMPANY_CNPJ") or "") if ch.isdigit()) or None
            company = Company(name=os.getenv("COMPANY_NAME") or "Empresa Principal", cnpj=cnpj)
            db.add(company)
            db.commit()
        cid = company.id
        auto_cfg = db.scalar(select(SyncAutomationConfig).where(SyncAutomationConfig.company_id == cid).limit(1))
        if not auto_cfg:
            db.add(SyncAutomationConfig(company_id=cid, active=False, poll_seconds=30))
        auto_defaults = {
            "products": (False, 360), "banks": (False, 720), "clientes": (False, 120),
            "orders": (True, 15), "faturamento": (True, 15), "receber": (True, 15),
            "pagar": (False, 30), "compras": (False, 30), "despesas": (False, 60),
        }
        for mod, (enabled, minutes) in auto_defaults.items():
            exists = db.scalar(select(SyncAutomationModule).where(
                SyncAutomationModule.company_id == cid, SyncAutomationModule.module == mod
            ).limit(1))
            if not exists:
                db.add(SyncAutomationModule(company_id=cid, module=mod, enabled=enabled, interval_minutes=minutes))
        default_regions = [
            ("Outras Regiões", 10), ("COLETA", 20), ("A Classificar", 30),
            ("Zona Oeste", 40), ("Centro", 50), ("Zona Sul", 60),
            ("Osasco / Barueri", 70), ("Zona Norte", 80), ("Zona Leste", 90),
            ("ABC Paulista", 100), ("Guarulhos", 110), ("Alto Tietê", 120),
            ("Interior SP", 130), ("Litoral SP", 140),
        ]
        for name, order in default_regions:
            exists = db.scalar(select(Region).where(Region.company_id == cid, Region.name == name).limit(1))
            if not exists:
                db.add(Region(company_id=cid, name=name, sort_order=order))
        for order, name in enumerate(("Papelão", "Caixa Vermelha"), 10):
            exists = db.scalar(select(BoxType).where(BoxType.company_id == cid, BoxType.name == name).limit(1))
            if not exists:
                db.add(BoxType(company_id=cid, name=name, sort_order=order))
        coleta = db.scalar(select(Responsible).where(Responsible.company_id == cid, Responsible.name == "COLETA").limit(1))
        if not coleta:
            db.add(Responsible(company_id=cid, name="COLETA", kind="COLETA", active=True))
        _seed_security(db, company)
        db.commit()


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def current_company_id(db) -> int:
    company = db.scalar(select(Company).where(Company.active.is_(True)).order_by(Company.id).limit(1))
    return company.id if company else 1
