from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

# IDs/nome de bancos confirmados na conta real usada pela plataforma.
# O catálogo sincronizado continua tendo prioridade; estes valores são apenas fallback seguro.
VERIFIED_BANK_NAMES = {
    "12626": "BRADESCO",
    "25325": "ITAU VIN",
    "32979": "SAFRA - BANCO",
}



def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def financial_bank_wallet_fields(record: Any) -> dict[str, str]:
    """Extrai banco/carteira sem confundir ID numérico com descrição.

    O ERPFlex varia entre campos explícitos (``id_banco``/``nome_banco``) e
    campos genéricos (``banco``/``carteira``). Em alguns retornos o campo
    genérico contém o ID interno, não o nome. Esta função preserva essa
    distinção para evitar gravar/mostrar ``25325`` como se fosse nome do banco.
    """
    payload = record if isinstance(record, (dict, list)) else {}

    def scalar(names: tuple[str, ...]) -> str:
        return text(deep_find(payload, names)).strip()

    bank_id = scalar((
        "id_banco", "banco_id", "idBanco", "bank_id", "idBank", "SA6_ID",
    ))
    bank_desc = scalar((
        "banco_desc", "nome_banco", "descricao_banco", "bank_name", "nomeBanco", "SA6_Desc",
    ))
    bank_generic = scalar(("banco", "bank"))
    if bank_generic:
        if re.fullmatch(r"\d+", bank_generic):
            bank_id = bank_id or bank_generic
        elif not bank_desc:
            bank_desc = bank_generic

    wallet_id = scalar((
        "id_carteira", "carteira_id", "idCarteira", "wallet_id", "idWallet",
    ))
    wallet_desc = scalar((
        "carteira_desc", "nome_carteira", "descricao_carteira", "wallet_name", "nomeCarteira",
    ))
    wallet_generic = scalar(("carteira", "wallet"))
    if wallet_generic:
        if re.fullmatch(r"\d+", wallet_generic):
            wallet_id = wallet_id or wallet_generic
        elif not wallet_desc:
            wallet_desc = wallet_generic

    # Se um campo rotulado como descrição vier numérico, ele é tecnicamente um ID.
    if bank_desc and re.fullmatch(r"\d+", bank_desc):
        bank_id = bank_id or bank_desc
        bank_desc = ""
    if wallet_desc and re.fullmatch(r"\d+", wallet_desc):
        wallet_id = wallet_id or wallet_desc
        wallet_desc = ""

    return {
        "bank_id": bank_id, "bank_desc": bank_desc,
        "wallet_id": wallet_id, "wallet_desc": wallet_desc,
    }


def fnum(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def normalize_date(value: Any) -> str | None:
    s = text(value)
    if not s:
        return None
    # Preserve API data without inventing conversions; normalize only obvious formats.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt).date().isoformat()
        except Exception:
            pass
    return s[:40]


def key_norm(value: Any) -> str:
    """Normaliza nomes de campos da API sem alterar os valores.

    O ERPFlex pode retornar a mesma informação com espaços, sublinhados,
    hífens, maiúsculas ou acentos diferentes (ex.: ``Endereco Entrega`` e
    ``endereco_entrega``). A normalização é usada somente para comparar chaves.
    """
    raw = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", raw)


def _case_get(d: dict, name: str):
    if name in d:
        return d.get(name)
    lname = str(name).lower()
    for k, v in d.items():
        if str(k).lower() == lname:
            return v
    wanted = key_norm(name)
    if wanted:
        for k, v in d.items():
            if key_norm(k) == wanted:
                return v
    return None


def pick(record: Any, *names: str, default=None):
    if not isinstance(record, dict):
        return default
    for name in names:
        if "." in name:
            cur: Any = record
            ok = True
            for part in name.split("."):
                if not isinstance(cur, dict):
                    ok = False
                    break
                cur = _case_get(cur, part)
                if cur is None:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        else:
            v = _case_get(record, name)
            if v not in (None, ""):
                return v
    return default


def deep_find(record: Any, names: tuple[str, ...]):
    if isinstance(record, dict):
        for name in names:
            v = _case_get(record, name)
            if v not in (None, "", [], {}):
                return v
        for v in record.values():
            found = deep_find(v, names)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(record, list):
        for v in record:
            found = deep_find(v, names)
            if found not in (None, "", [], {}):
                return found
    return None


def extract_records(payload: Any, preferred: tuple[str, ...] = ()) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    keys = preferred + ("data", "dados", "items", "results", "result", "registros", "records", "lista")
    for key in keys:
        v = _case_get(payload, key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            nested = extract_records(v, preferred)
            if nested:
                return nested
    # Last resort: choose the largest list of objects in the envelope.
    candidates = []
    for v in payload.values():
        if isinstance(v, list) and all(isinstance(x, dict) for x in v):
            candidates.append(v)
    if candidates:
        return max(candidates, key=len)
    # A detail endpoint can return one object.
    if any(k.lower() in {"id", "codigo", "nfe", "documento", "chave_nfe", "chave_acesso"} for k in payload.keys()):
        return [payload]
    return []


def external_id(module: str, record: dict) -> str:
    module = module.lower()
    candidates = {
        "products": ("id", "id_produto", "codigo", "codigo_produto", "sku"),
        "orders": ("id", "solicitacao_id", "pedido_id", "id_solicitacao", "numero_pedido", "pedido", "codigo"),
        "faturamento": ("faturamento_id", "id", "id_faturamento", "id_nota", "nfe", "documento", "codigo_autenticacao_digital", "chave_nfe"),
        "receber": ("id", "id_titulo", "id_receita", "documento", "numero"),
        "pagar": ("id", "id_titulo", "id_despesa", "documento", "numero"),
        "compras": ("id", "compra_id", "id_compra", "id_informacoes_fiscais", "codigo_autenticacao_digital", "chave_nfe", "nfe", "documento"),
        "despesas": ("id", "id_despesa", "documento", "numero"),
        "banks": ("SA6_ID", "id", "id_banco", "codigo", "SA6_Codigo", "nome", "SA6_Desc"),
        "clientes": ("id", "id_cliente", "cliente_id", "codigo", "cnpj", "cpf_cnpj"),
        "fornecedores": ("id", "id_fornecedor", "fornecedor_id", "codigo", "cnpjcpf", "cnpj_cpf", "cnpj"),
        "nfstock": ("nsu", "NSU", "chave", "chaveAcesso", "chave_nfe"),
    }.get(module, ("id", "codigo", "numero", "documento"))
    v = deep_find(record, candidates)
    if v not in (None, ""):
        return str(v)
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def payload_hash(record: Any) -> str:
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def access_key_from(record: dict) -> str | None:
    value = deep_find(record, (
        "codigo_autenticacao_digital", "chave_nfe", "chave_acesso", "chaveAcesso", "chave", "chNFe"
    ))
    d = digits(value)
    return d if len(d) == 44 else None


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
