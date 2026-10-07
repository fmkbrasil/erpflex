from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DATA_DIR
from .db import Company, EmailConfig, IntegrationConfig

_DEFAULT_SECRET = "desenvolvimento-troque-em-producao"
_LOCAL_KEY_FILE = DATA_DIR / "integration.key"


def _railway_or_remote_db() -> bool:
    db = (os.getenv("DATABASE_URL") or "").lower()
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID") or db.startswith("postgres"))


def _local_persistent_key() -> str:
    try:
        if _LOCAL_KEY_FILE.exists():
            value = _LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        _LOCAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_KEY_FILE.write_text(value, encoding="utf-8")
        try:
            _LOCAL_KEY_FILE.chmod(0o600)
        except Exception:
            pass
        return value
    except Exception:
        return (os.getenv("SECRET_KEY") or _DEFAULT_SECRET).strip()


def encryption_source() -> str:
    if (os.getenv("INTEGRATION_SECRET_KEY") or "").strip():
        return "INTEGRATION_SECRET_KEY"
    if _railway_or_remote_db():
        return "SECRET_KEY"
    return f"arquivo persistente: {_LOCAL_KEY_FILE}"


def _master_secret() -> str:
    explicit = (os.getenv("INTEGRATION_SECRET_KEY") or "").strip()
    if explicit:
        return explicit
    if _railway_or_remote_db():
        return (os.getenv("SECRET_KEY") or _DEFAULT_SECRET).strip()
    return _local_persistent_key()


def encryption_fingerprint() -> str:
    return hashlib.sha256(_master_secret().encode("utf-8")).hexdigest()[:12]


def encryption_is_default() -> bool:
    return _master_secret() == _DEFAULT_SECRET


def _fernet() -> Fernet:
    raw = hashlib.sha256(_master_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def _row(db: Session, company_id: int, provider: str) -> IntegrationConfig | None:
    return db.scalar(select(IntegrationConfig).where(
        IntegrationConfig.company_id == company_id,
        IntegrationConfig.provider == provider.upper(),
    ).limit(1))


def get_erpflex_settings(db: Session, company_id: int) -> dict[str, Any]:
    row = _row(db, company_id, "ERPFLEX")
    if row:
        return {
            "source": "database",
            "active": bool(row.active),
            "base_url": (row.base_url or "https://api.erpflex.com.br").rstrip("/"),
            "username": row.username or "",
            "password": decrypt_secret(row.secret_encrypted),
            "secret_stored": bool(row.secret_encrypted),
            "row": row,
        }
    return {
        "source": "environment",
        "active": True,
        "base_url": (os.getenv("ERPFLEX_API_BASE") or "https://api.erpflex.com.br").rstrip("/"),
        "username": os.getenv("ERPFLEX_USER") or "",
        "password": os.getenv("ERPFLEX_PASS") or "",
        "secret_stored": bool(os.getenv("ERPFLEX_PASS")),
        "row": None,
    }


def get_nfstock_settings(db: Session, company_id: int) -> dict[str, Any]:
    row = _row(db, company_id, "NFSTOCK")
    company = db.get(Company, company_id)
    company_cnpj = "".join(ch for ch in ((company.cnpj if company else "") or "") if ch.isdigit())
    if row:
        return {
            "source": "database",
            "active": bool(row.active),
            "base_url": (row.base_url or "https://ms-exportacao-nfstock.pack.alterdata.com.br").rstrip("/"),
            "token": decrypt_secret(row.secret_encrypted),
            "secret_stored": bool(row.secret_encrypted),
            "crm": (row.crm or "").zfill(6) if row.crm else "",
            "cnpj": "".join(ch for ch in (row.company_cnpj or company_cnpj) if ch.isdigit()),
            "page_size": int(row.page_size or 25),
            "row": row,
        }
    return {
        "source": "environment",
        "active": True,
        "base_url": (os.getenv("NFSTOCK_BASE_URL") or "https://ms-exportacao-nfstock.pack.alterdata.com.br").rstrip("/"),
        "token": os.getenv("NFSTOCK_TOKEN") or "",
        "secret_stored": bool(os.getenv("NFSTOCK_TOKEN")),
        "crm": (os.getenv("NFSTOCK_CRM") or "").zfill(6) if os.getenv("NFSTOCK_CRM") else "",
        "cnpj": "".join(ch for ch in (os.getenv("COMPANY_CNPJ") or company_cnpj) if ch.isdigit()),
        "page_size": max(1, min(100, int(os.getenv("NFSTOCK_PAGE_SIZE") or 25))),
        "row": None,
    }


def save_erpflex(db: Session, company_id: int, *, base_url: str, username: str, password: str, active: bool) -> IntegrationConfig:
    row = _row(db, company_id, "ERPFLEX")
    if not row:
        row = IntegrationConfig(company_id=company_id, provider="ERPFLEX")
        db.add(row)
    row.active = active
    row.base_url = (base_url or "https://api.erpflex.com.br").strip().rstrip("/")
    row.username = username.strip()
    if password:
        row.secret_encrypted = encrypt_secret(password)
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def save_nfstock(db: Session, company_id: int, *, base_url: str, token: str, crm: str, cnpj: str, page_size: int, active: bool) -> IntegrationConfig:
    row = _row(db, company_id, "NFSTOCK")
    if not row:
        row = IntegrationConfig(company_id=company_id, provider="NFSTOCK")
        db.add(row)
    row.active = active
    row.base_url = (base_url or "https://ms-exportacao-nfstock.pack.alterdata.com.br").strip().rstrip("/")
    row.crm = "".join(ch for ch in (crm or "") if ch.isdigit())[:20]
    row.company_cnpj = "".join(ch for ch in (cnpj or "") if ch.isdigit())[:20]
    row.page_size = max(1, min(100, int(page_size or 25)))
    if token:
        row.secret_encrypted = encrypt_secret(token)
    row.updated_at = datetime.utcnow()
    db.flush()
    return row



def get_smtp_settings(db: Session, company_id: int) -> dict[str, Any]:
    row = db.scalar(select(EmailConfig).where(EmailConfig.company_id == company_id).limit(1))
    if row:
        password = decrypt_secret(row.secret_encrypted)
        host = row.host or ""; username = row.username or ""; from_email = row.from_email or username or ""
        active = bool(row.active)
        return {
            "source": "database", "active": active, "host": host,
            "port": int(row.port or 587), "username": username,
            "password": password, "secret_stored": bool(row.secret_encrypted),
            "from_email": from_email, "from_name": row.from_name or "",
            "security": (row.security or "starttls").lower(), "row": row,
            "configured": bool(active and host and from_email and (password or not username)),
        }
    host = os.getenv("SMTP_HOST") or ""; username = os.getenv("SMTP_USER") or ""; password = os.getenv("SMTP_PASSWORD") or ""
    from_email = os.getenv("SMTP_FROM") or username or ""
    return {
        "source": "environment", "active": True, "host": host,
        "port": int(os.getenv("SMTP_PORT") or 587), "username": username,
        "password": password, "secret_stored": bool(password),
        "from_email": from_email, "from_name": os.getenv("SMTP_FROM_NAME") or "",
        "security": (os.getenv("SMTP_SECURITY") or "starttls").lower(), "row": None,
        "configured": bool(host and from_email and (password or not username)),
    }


def save_smtp(db: Session, company_id: int, *, host: str, port: int, username: str, password: str,
              from_email: str, from_name: str, security: str, active: bool) -> EmailConfig:
    row = db.scalar(select(EmailConfig).where(EmailConfig.company_id == company_id).limit(1))
    if not row:
        row = EmailConfig(company_id=company_id)
        db.add(row)
    row.active = bool(active)
    row.host = (host or "").strip()
    row.port = max(1, min(65535, int(port or 587)))
    row.username = (username or "").strip()
    if password:
        row.secret_encrypted = encrypt_secret(password)
    row.from_email = (from_email or username or "").strip()
    row.from_name = (from_name or "").strip()
    row.security = security if security in {"starttls", "ssl", "none"} else "starttls"
    row.updated_at = datetime.utcnow()
    db.flush()
    return row

def mark_test_result(db: Session, company_id: int, provider: str, ok: bool, message: str) -> None:
    row = _row(db, company_id, provider)
    if not row:
        return
    row.last_test_ok = bool(ok)
    row.last_test_at = datetime.utcnow()
    row.last_test_message = (message or "")[:2000]
    db.flush()


def integration_public_view(db: Session, company_id: int) -> dict[str, dict[str, Any]]:
    erp = get_erpflex_settings(db, company_id)
    nf = get_nfstock_settings(db, company_id)
    smtp = get_smtp_settings(db, company_id)
    erp_row = erp.get("row")
    nf_row = nf.get("row")
    smtp_row = smtp.get("row")
    return {
        "erpflex": {
            "active": erp["active"], "base_url": erp["base_url"], "username": erp["username"],
            "configured": bool(erp["active"] and erp["base_url"] and erp["username"] and erp["password"]),
            "secret_stored": erp["secret_stored"], "source": erp["source"],
            "last_test_ok": erp_row.last_test_ok if erp_row else None,
            "last_test_at": erp_row.last_test_at if erp_row else None,
            "last_test_message": erp_row.last_test_message if erp_row else None,
        },
        "nfstock": {
            "active": nf["active"], "base_url": nf["base_url"], "crm": nf["crm"], "cnpj": nf["cnpj"],
            "page_size": nf["page_size"],
            "configured": bool(nf["active"] and nf["base_url"] and nf["token"] and nf["crm"] and nf["cnpj"]),
            "secret_stored": nf["secret_stored"], "source": nf["source"],
            "last_test_ok": nf_row.last_test_ok if nf_row else None,
            "last_test_at": nf_row.last_test_at if nf_row else None,
            "last_test_message": nf_row.last_test_message if nf_row else None,
        },
        "smtp": {
            "active": smtp["active"], "host": smtp["host"], "port": smtp["port"],
            "username": smtp["username"], "from_email": smtp["from_email"], "from_name": smtp["from_name"],
            "security": smtp["security"], "source": smtp["source"], "secret_stored": smtp["secret_stored"],
            "configured": bool(smtp["active"] and smtp["host"] and smtp["port"] and smtp["from_email"] and (smtp["password"] or not smtp["username"])),
            "last_test_ok": getattr(smtp_row, "last_test_ok", None) if smtp_row else None,
            "last_test_at": getattr(smtp_row, "last_test_at", None) if smtp_row else None,
            "last_test_message": getattr(smtp_row, "last_test_message", None) if smtp_row else None,
        },
    }
