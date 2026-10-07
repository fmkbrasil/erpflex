from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"
# Variáveis do ambiente (Railway/Windows) têm prioridade sobre o arquivo .env.
load_dotenv(ENV_FILE, override=False)


def _default_data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    local = (os.getenv("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "PlataformaGestaoIntegrada" / "data"
    return Path.home() / ".plataforma_gestao_integrada" / "data"


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_DATA_DIR = PROJECT_DIR / "data"


def migrate_legacy_sqlite_if_needed() -> Path | None:
    """Copia uma base SQLite legada para o diretório persistente na primeira execução.

    Procura primeiro a pasta ``data`` da própria versão e, depois, versões irmãs
    ``Plataforma_Gestao_Integrada*`` no mesmo diretório. Se houver mais de uma,
    usa a base modificada mais recentemente. A origem nunca é apagada.
    """
    if (os.getenv("DATABASE_URL") or "").strip():
        return None
    target = DATA_DIR / "plataforma_integrada.db"
    if target.exists():
        return None

    candidates: list[Path] = []
    direct = LEGACY_DATA_DIR / "plataforma_integrada.db"
    if direct.exists():
        candidates.append(direct)

    parent = PROJECT_DIR.parent
    try:
        for folder in parent.glob("Plataforma_Gestao_Integrada*"):
            if not folder.is_dir() or folder.resolve() == PROJECT_DIR.resolve():
                continue
            db = folder / "data" / "plataforma_integrada.db"
            if db.exists():
                candidates.append(db)
    except Exception:
        pass

    candidates = [c for c in candidates if c.exists() and c.resolve() != target.resolve()]
    if not candidates:
        return None
    legacy = max(candidates, key=lambda x: x.stat().st_mtime)
    shutil.copy2(legacy, target)
    try:
        (DATA_DIR / "legacy_migration_source.txt").write_text(str(legacy), encoding="utf-8")
    except Exception:
        pass
    return target
