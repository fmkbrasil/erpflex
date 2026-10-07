from __future__ import annotations

import base64
import io
import os
import time
import zipfile
import json
import platform
import subprocess
from pathlib import Path
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import urljoin, quote

import httpx

from . import config as _config  # carrega .env antes de ler fallbacks de ambiente
from .utils import access_key_from, deep_find, extract_records, normalize_date, pick


class ConnectorError(RuntimeError):
    pass


@dataclass
class APIPage:
    records: list[dict]
    http: int
    url: str
    payload: Any = None
    duration_ms: int = 0
    body_excerpt: str = ""


def _extract_best_array(payload: Any) -> list[dict]:
    """Replica o extractBestArray do ERPFlex Analytics V7.8.

    A API pode devolver mais de um array no mesmo envelope. Para os endpoints
    paginados gerais, o V7.8 escolhe o maior array de objetos em qualquer nível.
    Isso evita confundir arrays auxiliares/metadados com a lista principal.
    """
    best: list[dict] = []

    def walk(value: Any) -> None:
        nonlocal best
        if isinstance(value, list):
            rows = [x for x in value if isinstance(x, dict)]
            if len(rows) > len(best):
                best = rows
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

    walk(payload)
    return best


def _extract_named_records(payload: Any, names: tuple[str, ...]) -> list[dict]:
    """Replica extractNamedRecords do Analytics V7.8 para endpoints de detalhe.

    Prioriza semanticamente chaves como compra/compras/data em vez de escolher
    cegamente o maior array, que em detalhes costuma ser justamente a lista de itens.
    """
    wanted = [str(n).lower() for n in names]

    def convert(value: Any) -> list[dict]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        return []

    if isinstance(payload, dict):
        for name in wanted:
            for key, value in payload.items():
                if str(key).lower() == name:
                    rows = convert(value)
                    if rows:
                        return rows

    best_rank = 10**9
    best: list[dict] = []

    def walk(value: Any) -> None:
        nonlocal best_rank, best
        if isinstance(value, dict):
            for key, child in value.items():
                lk = str(key).lower()
                if lk in wanted:
                    rows = convert(child)
                    rank = wanted.index(lk)
                    if rows and rank < best_rank:
                        best_rank, best = rank, rows
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return best


def _find_named_object_list(payload: Any, names: tuple[str, ...]) -> list[dict]:
    wanted = {n.lower() for n in names}
    found: list[dict] = []
    def walk(value: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in wanted and isinstance(child, list):
                    rows = [x for x in child if isinstance(x, dict)]
                    if rows:
                        found = rows
                        return
                walk(child)
                if found:
                    return
        elif isinstance(value, list):
            for child in value:
                walk(child)
                if found:
                    return
    walk(payload)
    return found




def _one_object_data(payload: Any) -> dict | None:
    """Replica oneObjectData do ERPFlex Analytics V7.8 para endpoints de detalhe."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return payload


class ERPFlexClient:
    """Cliente somente leitura baseado no motor comprovado do ERPFlex Analytics V7.8.

    Pontos preservados do V7.8:
    - mesmos endpoints;
    - cursores/offsets de referência;
    - paginação financeira por posição numérica;
    - busca binária por período;
    - HTTP 4xx/5xx de borda são avaliados pelo algoritmo (não viram erro de transporte);
    - conexões HTTP reaproveitadas e timeout de leitura maior para faturamento.
    """

    ORDER_SEED = 158000
    FATURAMENTO_SEED = 15983
    RECEBER_FIRST_INVALID_SEED = 174685

    def __init__(self, settings: dict | None = None, progress: Callable[[str], None] | None = None, page_observer: Callable[[str, str, int, list[dict]], None] | None = None):
        settings = settings or {}
        self._progress = progress
        self._page_observer = page_observer
        self.active = bool(settings.get("active", True))
        self.base = (settings.get("base_url") or os.getenv("ERPFLEX_API_BASE") or "https://api.erpflex.com.br").rstrip("/")
        self.user = settings.get("username") if "username" in settings else (os.getenv("ERPFLEX_USER") or "")
        self.password = settings.get("password") if "password" in settings else (os.getenv("ERPFLEX_PASS") or "")
        timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
        limits = httpx.Limits(max_connections=30, max_keepalive_connections=12, keepalive_expiry=90.0)
        self._client = httpx.Client(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            auth=(self.user, self.password),
            headers={"Accept": "application/json", "User-Agent": "Go-http-client/1.1"},
        )

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @property
    def configured(self) -> bool:
        return bool(self.active and self.base and self.user and self.password)

    def _notify(self, message: str) -> None:
        if self._progress:
            try:
                self._progress(message)
            except Exception:
                pass

    def _observe_page(self, module: str, cursor_kind: str, cursor_value: int, page: APIPage) -> None:
        if not self._page_observer or not page or page.http != 200 or not page.records:
            return
        try:
            self._page_observer(module, cursor_kind, int(cursor_value), page.records)
        except Exception:
            # O índice é uma otimização e nunca pode interromper a sincronização.
            pass

    def _bridge_binary(self) -> Path | None:
        """Localiza o motor HTTP Go derivado do ERPFlex Analytics V7.8."""
        root = Path(__file__).resolve().parent.parent
        system = platform.system().lower()
        candidates = []
        if system.startswith("win"):
            candidates.append(root / "bin" / "erpflex_v78_bridge_windows.exe")
        else:
            candidates.append(root / "bin" / "erpflex_v78_bridge")
        override = (os.getenv("ERPFLEX_V78_BRIDGE") or "").strip()
        if override:
            candidates.insert(0, Path(override))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _get_go_v78(self, path: str) -> APIPage:
        binary = self._bridge_binary()
        if binary is None:
            raise ConnectorError("Motor ERPFlex V7.8 em Go não encontrado no pacote.")
        request = {
            "base_url": self.base,
            "username": self.user,
            "password": self.password,
            "path": path,
            "accept": "application/json",
        }
        try:
            proc = subprocess.run(
                [str(binary)],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=150,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ConnectorError(f"Timeout do motor Go V7.8 em {path} após 150 s.") from e
        except Exception as e:
            raise ConnectorError(f"Falha ao iniciar motor Go V7.8 em {path}: {e}") from e
        raw = (proc.stdout or "").strip()
        try:
            result = json.loads(raw) if raw else {}
        except Exception as e:
            detail = ((proc.stderr or "") + " " + raw)[:500]
            raise ConnectorError(f"Resposta inválida do motor Go V7.8 em {path}: {detail}") from e
        if not result.get("ok"):
            detail = result.get("error") or (proc.stderr or "").strip() or f"código {proc.returncode}"
            raise ConnectorError(f"Motor Go V7.8 falhou em {path}: {detail}")
        payload = result.get("payload")
        records = result.get("records") or []
        preferred = ()
        if path.startswith("/api/compra/"):
            preferred = ("compras", "compra", "confirmados", "data")
        elif path.startswith("/api_v2/despesa"):
            preferred = ("despesas", "despesa", "data")
        if preferred:
            named = extract_records(payload, preferred=preferred)
            if named:
                records = named
        return APIPage(
            records=[x for x in records if isinstance(x, dict)],
            http=int(result.get("http") or 0),
            url=str(result.get("url") or ""),
            payload=payload,
            duration_ms=int(result.get("duration_ms") or 0),
            body_excerpt=str(result.get("body_excerpt") or "")[:400],
        )


    def _raw_go_v78(self, path: str, accept: str = "application/json") -> tuple[int, str, bytes]:
        binary = self._bridge_binary()
        if binary is None:
            raise ConnectorError("Motor ERPFlex V7.8 em Go não encontrado no pacote.")
        request = {
            "base_url": self.base, "username": self.user, "password": self.password,
            "path": path, "accept": accept or "application/json",
        }
        try:
            proc = subprocess.run(
                [str(binary)], input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
                errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ConnectorError(f"Timeout do motor Go V7.8 em {path} após 150 s.") from e
        raw = (proc.stdout or "").strip()
        try:
            result = json.loads(raw) if raw else {}
        except Exception as e:
            raise ConnectorError(f"Resposta inválida do motor Go V7.8 em {path}") from e
        if not result.get("ok"):
            raise ConnectorError(f"Motor Go V7.8 falhou em {path}: {result.get('error') or proc.stderr or proc.returncode}")
        body = b""
        if result.get("body_base64"):
            try:
                body = base64.b64decode(result.get("body_base64"))
            except Exception:
                body = b""
        elif result.get("payload") is not None:
            body = json.dumps(result.get("payload"), ensure_ascii=False).encode("utf-8")
        return int(result.get("http") or 0), str(result.get("content_type") or ""), body

    def _get_httpx(self, path: str, *, read_timeout: float | None = None) -> APIPage:
        url = self.base + (path if path.startswith("/") else "/" + path)
        started = time.monotonic()
        try:
            if read_timeout is None:
                r = self._client.get(url)
            else:
                timeout = httpx.Timeout(connect=15.0, read=max(5.0, float(read_timeout)), write=30.0, pool=30.0)
                r = self._client.get(url, timeout=timeout)
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            raise ConnectorError(f"Timeout ERPFlex em {path}: {e}") from e
        except (httpx.RemoteProtocolError, httpx.ConnectError) as e:
            raise ConnectorError(f"Falha de conexão ERPFlex em {path}: {e}") from e
        except Exception as e:
            raise ConnectorError(f"Falha de conexão ERPFlex em {path}: {e}") from e
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            payload = r.json()
        except Exception:
            payload = None
        preferred = ()
        if path.startswith("/api/compra/"):
            preferred = ("compras", "compra", "confirmados", "data")
        elif path.startswith("/api_v2/despesa"):
            preferred = ("despesas", "despesa", "data")
        if preferred:
            records = extract_records(payload, preferred=preferred)
            if not records:
                records = _extract_best_array(payload)
        else:
            records = _extract_best_array(payload)
            if not records:
                records = extract_records(payload)
        return APIPage(records=records, http=r.status_code, url=str(r.url), payload=payload,
                       duration_ms=duration_ms, body_excerpt=(r.text or "")[:400])

    def _get(self, path: str, *, read_timeout: float | None = None) -> APIPage:
        if not self.configured:
            raise ConnectorError("Configure ERPFLEX_USER e ERPFLEX_PASS.")
        # V1.3.15: por padrão TODAS as chamadas ERPFlex passam pelo mesmo motor
        # HTTP Go do Analytics V7.8. O fallback Python existe apenas para diagnóstico.
        engine = (os.getenv("ERPFLEX_ENGINE") or "go_v78").strip().lower()
        if engine in {"go", "go_v78", "v78"}:
            return self._get_go_v78(path)
        return self._get_httpx(path, read_timeout=read_timeout)

    @staticmethod
    def _ok(page: APIPage) -> bool:
        return 200 <= page.http < 300 and bool(page.records)

    @staticmethod
    def _as_date(value: Any) -> date | None:
        raw = normalize_date(value)
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            return None

    @classmethod
    def _record_date(cls, module: str, record: dict, navigation: bool = False) -> date | None:
        # Mesma prioridade de campos do Analytics V7.8. Primeiro procura no
        # cabeçalho do registro; só usa busca profunda como fallback.
        if module == "orders":
            names = ("data_inclusao", "dt_inclusao", "emissao") if navigation else ("emissao", "data_inclusao", "dt_inclusao")
        elif module == "faturamento":
            names = ("data_emissao", "dt_inclusao")
        elif module == "compras":
            names = ("emissao", "emissao_original", "data_saida")
        elif module == "despesas":
            names = ("data_emissao",)
        else:
            names = ("vencimento", "data_baixa")
        value = pick(record, *names)
        if value in (None, ""):
            value = deep_find(record, names)
        return cls._as_date(value)

    @classmethod
    def _range(cls, module: str, rows: list[dict], navigation: bool = False) -> tuple[date | None, date | None]:
        vals = [cls._record_date(module, r, navigation=navigation) for r in rows]
        vals = [d for d in vals if d]
        return (min(vals), max(vals)) if vals else (None, None)

    @classmethod
    def _filter_period(cls, module: str, rows: list[dict], start_date: str, end_date: str) -> list[dict]:
        try:
            start = date.fromisoformat(start_date[:10])
            end = date.fromisoformat(end_date[:10])
        except Exception as e:
            raise ConnectorError("Período inválido para sincronização ERPFlex.") from e
        if end < start:
            start, end = end, start
        out = []
        for row in rows:
            d = cls._record_date(module, row, navigation=False)
            if d and start <= d <= end:
                out.append(row)
        return out

    def test(self) -> dict:
        page = self._get("/api/bancos/")
        return {
            "ok": 200 <= page.http < 300,
            "http": page.http,
            "records": len(page.records),
            "url": page.url,
            "duration_ms": page.duration_ms,
        }

    def _offset_valid(self, prefix: str, offset: int) -> bool:
        path = prefix + (str(offset) if offset else "")
        return self._ok(self._get(path))

    def discover_last_offset(self, prefix: str, step: int = 10, max_calls: int = 40, seed: int = 0) -> int:
        """Descobre o último offset válido; HTTP de borda é tratado como inválido, não exceção."""
        low = max(0, int(seed or 0))
        high = low + max(1000, step)
        calls = 0
        if low > 0:
            calls += 1
            if not self._offset_valid(prefix, low):
                low = 0
                high = max(1000, step)
        while calls < max_calls:
            ok = self._offset_valid(prefix, high)
            calls += 1
            if not ok:
                break
            low = high
            high = low + max(1000, (high - max(0, low // 2)) * 2)
        while high - low > step and calls < max_calls:
            mid = ((low + high) // (2 * step)) * step
            if mid <= low:
                mid = low + step
            ok = self._offset_valid(prefix, mid)
            calls += 1
            if ok:
                low = mid
            else:
                high = mid
        return max(0, low)

    def discover_orders_end(self, seed: int | None = None, max_calls: int = 60) -> int:
        """Primeiro offset inválido de pedidos, seguindo o algoritmo do V7.8."""
        low = max(0, int(seed if seed is not None else self.ORDER_SEED))
        step = 1000
        calls = 0

        def valid(off: int) -> bool:
            nonlocal calls
            path = "/api/venda/solicitacoes/" + (str(off) if off > 0 else "")
            page = self._get(path)
            calls += 1
            return self._ok(page)

        if not valid(low):
            low = 0
        high = low + step
        while calls < max_calls and valid(high):
            low = high
            step *= 2
            high = low + step
        while high - low > 10 and calls < max_calls:
            mid = ((low + high) // 20) * 10
            if mid <= low:
                mid = low + 10
            if valid(mid):
                low = mid
            else:
                high = mid
        return high

    def discover_faturamento_last_page(self, seed: int | None = None, max_calls: int = 60) -> int:
        """Localiza a última página válida, usando como âncora o estado validado no V7.8."""
        seed = max(1, int(seed or self.FATURAMENTO_SEED))
        calls = 0

        def valid(page_no: int) -> bool:
            nonlocal calls
            if page_no < 1 or calls >= max_calls:
                return False
            page = self._get(f"/api_v2/faturamento/P{page_no}")
            calls += 1
            return self._ok(page)

        anchors: list[int] = []
        for n in (seed, self.FATURAMENTO_SEED, 15000, 10000, 5000, 1000, 100, 10, 1):
            if n > 0 and n not in anchors:
                anchors.append(n)
        low = 0
        for anchor in anchors:
            if calls >= max_calls:
                break
            if valid(anchor):
                low = anchor
                break
        if low <= 0:
            return 0
        step = 1
        high = low + step
        while calls < max_calls and valid(high):
            low = high
            step *= 2
            high = low + step
        while high - low > 1 and calls < max_calls:
            mid = (low + high) // 2
            if valid(mid):
                low = mid
            else:
                high = mid
        return low

    def discover_finance_end(self, prefix: str, max_calls: int = 60, seed: int | None = None) -> int:
        """Retorna a primeira posição inválida. Nunca chama o prefixo puro /pagina/."""
        max_calls = max(20, max_calls)
        calls = 0

        def valid(pos: int) -> bool:
            nonlocal calls
            page = self._get(prefix + str(max(1, int(pos))))
            calls += 1
            return self._ok(page)

        seed = int(seed or 0)
        if seed > 1:
            # Cursor do V7.8 costuma apontar para a primeira posição inválida.
            if not valid(seed):
                prev = max(1, seed - 10)
                if valid(prev):
                    return seed
                # Cursor muito à frente/obsoleto: redescobre desde o início.
            else:
                low = seed
                step = 100
                high = low + step
                while calls < max_calls and valid(high):
                    low = high
                    step *= 2
                    high = low + step
                while high - low > 1 and calls < max_calls:
                    mid = (low + high) // 2
                    if valid(mid):
                        low = mid
                    else:
                        high = mid
                return high

        low, high = 1, 10
        while calls < max_calls and valid(high):
            low = high
            high *= 2
        while high - low > 1 and calls < max_calls:
            mid = (low + high) // 2
            if valid(mid):
                low = mid
            else:
                high = mid
        return high

    def banks(self) -> list[dict]:
        page = self._get("/api/bancos/")
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Bancos: HTTP {page.http}")
        return page.records


    def order_detail(self, order_id: str) -> dict | None:
        """Consulta individual documentada do pedido/solicitação de venda.

        Endpoint oficial já validado no projeto de Pedidos ERPFlex:
        GET /api/venda/solicitacao/{id}
        O detalhe é a fonte correta para obter os itens quando o faturamento
        referencia um orcamento_id/pedido_id/solicitacao_id.
        """
        oid = str(order_id or "").strip()
        if not oid:
            return None
        page = self._get("/api/venda/solicitacao/" + quote(oid, safe=""))
        if page.http in (400, 404):
            return None
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Pedido {oid}: HTTP {page.http}")
        obj = _one_object_data(page.payload)
        if isinstance(obj, dict) and obj:
            return obj
        if page.records:
            return dict(page.records[0])
        return None

    def bank_detail(self, bank_id: str) -> dict | None:
        """Consulta o cadastro individual do banco pelo ID interno do ERPFlex."""
        bid = str(bank_id or "").strip()
        if not bid or bid == "0":
            return None
        page = self._get("/api/banco/" + quote(bid, safe=""))
        if page.http in (400, 404):
            return None
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Banco {bid}: HTTP {page.http}")
        return _one_object_data(page.payload)

    def faturamento_items(self, faturamento_id: str) -> dict | None:
        """Mantido apenas por compatibilidade interna.

        Não realiza chamada HTTP porque `/api_v2/faturamento/itens/{id}` não está
        validado na documentação nem no Analytics V7.8. Para obter produtos do
        faturamento, use o pedido relacionado através de `order_detail`.
        """
        return None

    def receivable_title_detail(self, title_id: str) -> dict | None:
        """Consulta Título a Receber V1, como o módulo Cobrança do Analytics V7.8."""
        tid = str(title_id or "").strip()
        if not tid:
            return None
        page = self._get("/api/titulo_receber/" + quote(tid, safe=""))
        if page.http in (400, 404):
            return None
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Título a Receber {tid}: HTTP {page.http}")
        return _one_object_data(page.payload)

    def customer_detail(self, customer_id: str) -> dict | None:
        page = self._get("/api/cliente/" + str(customer_id))
        if page.http in (400, 404):
            return None
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Cliente {customer_id}: HTTP {page.http}")
        if isinstance(page.payload, dict) and page.payload:
            if len(page.records) == 1:
                return page.records[0]
            return page.payload
        return page.records[0] if page.records else None

    def boleto_options(self) -> dict | list | None:
        page = self._get("/api_v2/boleto/options")
        if page.http in (400, 404):
            return None
        if page.http < 200 or page.http >= 300:
            raise ConnectorError(f"Opções de boleto: HTTP {page.http}")
        return page.payload

    def boleto_html(self, id_registro: str, id_banco: str, id_carteira: str) -> str:
        from urllib.parse import urlencode
        params = urlencode({
            "idRegistro": str(id_registro or "").strip(),
            "idBanco": str(id_banco or "").strip(),
            "idCarteira": str(id_carteira or "").strip(),
            "tipo": "html",
        })
        if not all([str(id_registro or "").strip(), str(id_banco or "").strip(), str(id_carteira or "").strip()]):
            raise ConnectorError("Boleto: idRegistro, idBanco e idCarteira são obrigatórios.")
        path = "/api_v2/boleto?" + params
        engine = (os.getenv("ERPFLEX_ENGINE") or "go_v78").strip().lower()
        if engine in {"go", "go_v78", "v78"}:
            http, content_type, body = self._raw_go_v78(path, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
            if http < 200 or http >= 300:
                raise ConnectorError(f"Boleto: HTTP {http}")
            html = body.decode("utf-8", errors="replace")
        else:
            url = self.base + path
            r = self._client.get(url, headers={"Accept":"text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"})
            if r.status_code < 200 or r.status_code >= 300:
                raise ConnectorError(f"Boleto: HTTP {r.status_code}")
            html = r.text
        if not html.strip():
            raise ConnectorError("Boleto: resposta vazia.")
        return html

    def products(self, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        offset = 0
        last_signature = ""
        for _ in range(max(1, max_pages)):
            page = self._get(f"/api/produtos/?limit=50&offset={offset}")
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"Produtos: HTTP {page.http} no offset {offset}")
            if not page.records:
                break
            signature = f"{len(page.records)}|{page.records[0]}|{page.records[-1]}"
            if last_signature and signature == last_signature:
                raise ConnectorError("Produtos: a API repetiu o mesmo bloco; paginação interrompida para evitar loop.")
            last_signature = signature
            out.extend(page.records)
            if len(page.records) < 50:
                break
            offset += len(page.records)
        return out

    def orders(self, max_blocks: int = 20, start_offset: int | None = None) -> list[dict]:
        out: list[dict] = []
        if start_offset is None:
            end = self.discover_orders_end(max_calls=max(30, min(60, max_blocks)))
            offset = max(0, end - 100)
        else:
            offset = max(0, int(start_offset or 0))
        for _ in range(max(1, max_blocks)):
            path = "/api/venda/solicitacoes/" + (str(offset) if offset else "")
            page = self._get(path)
            self._observe_page("orders", "offset", offset, page)
            if page.http in (400, 404) or not page.records:
                break
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"Pedidos: HTTP {page.http} no offset {offset}")
            out.extend(page.records)
            if len(page.records) < 10:
                break
            offset += 10
        return out

    def orders_period(self, start_date: str, end_date: str, max_blocks: int = 100, end_offset: int | None = None, page_hint: dict | None = None) -> tuple[list[dict], dict]:
        try:
            start = date.fromisoformat(start_date[:10]); end_date_obj = date.fromisoformat(end_date[:10])
        except Exception as e:
            raise ConnectorError("Período inválido para Pedidos.") from e
        if end_date_obj < start:
            start, end_date_obj = end_date_obj, start
        edge = int(end_offset or 0)
        # O cursor persistente representa a borda conhecida na execução anterior.
        # Revalida a partir do último bloco válido para capturar novos pedidos.
        seed = max(0, edge - 10) if edge > 0 else self.ORDER_SEED
        edge = self.discover_orders_end(seed=seed, max_calls=max(30, min(max_blocks, 60)))
        locate_calls = 0
        if page_hint and page_hint.get("min_cursor") is not None and page_hint.get("max_cursor") is not None:
            start_pos = max(0, (int(page_hint["min_cursor"]) // 10) * 10 - 20)
            cached_end = max(start_pos, (int(page_hint["max_cursor"]) // 10) * 10 + 20)
            # Se o período alcança a cobertura mais recente, inclui a borda atual para
            # capturar pedidos novos; em histórico fechado usa somente o índice local.
            end_pos = min(max(0, edge - 10), max(cached_end, max(0, edge - 10) if page_hint.get("touches_recent") else cached_end))
            self._notify(f"Pedidos · índice local {start_pos}→{end_pos} ({page_hint.get('count', 0)} bloco(s) conhecido(s))")
        else:
            low, high = 0, max(0, edge - 10)
            while high - low > 10 and locate_calls < 30:
                mid = ((low + high) // 20) * 10
                if mid <= low:
                    mid = low + 10
                page = self._get("/api/venda/solicitacoes/" + (str(mid) if mid else ""))
                self._observe_page("orders", "offset", mid, page)
                locate_calls += 1
                _, max_d = self._range("orders", page.records, navigation=True)
                if max_d is None or max_d < start:
                    low = mid
                else:
                    high = mid
            start_pos = max(0, high - 20)
            lo2, hi2 = start_pos, max(start_pos, edge - 10)
            while hi2 - lo2 > 10 and locate_calls < 60:
                mid = ((lo2 + hi2) // 20) * 10
                if mid <= lo2:
                    mid = lo2 + 10
                page = self._get("/api/venda/solicitacoes/" + (str(mid) if mid else ""))
                self._observe_page("orders", "offset", mid, page)
                locate_calls += 1
                min_d, _ = self._range("orders", page.records, navigation=True)
                if min_d is not None and min_d > end_date_obj:
                    hi2 = mid
                else:
                    lo2 = mid
            end_pos = min(max(0, edge - 10), hi2 + 20)
        expected = ((end_pos - start_pos) // 10) + 1 if end_pos >= start_pos else 0
        if expected > max_blocks:
            raise ConnectorError(f"Pedidos: período exige ~{expected} blocos; aumente o limite (atual {max_blocks}).")
        out: list[dict] = []
        calls = 0
        for pos in range(start_pos, end_pos + 1, 10):
            path = "/api/venda/solicitacoes/" + (str(pos) if pos else "")
            page = self._get(path); calls += 1
            self._observe_page("orders", "offset", pos, page)
            if page.http != 200 or not page.records:
                continue
            out.extend(self._filter_period("orders", page.records, start.isoformat(), end_date_obj.isoformat()))
        return out, {"initialized": True, "next_offset": edge, "last_http_requests": calls + locate_calls,
                     "coverage_from": start.isoformat(), "coverage_to": end_date_obj.isoformat(), "coverage_complete": True}

    def faturamento(self, max_pages: int = 20, start_page: int | None = None, last_valid: int | None = None) -> list[dict]:
        """Sincronização incremental fiel ao V7.8: parte da última página conhecida e avança."""
        out: list[dict] = []
        page = max(1, int(last_valid or self.FATURAMENTO_SEED)) if start_page is None else max(1, int(start_page or 1))
        start = max(1, page - 1) if start_page is None else page
        for i, pno in enumerate(range(start, start + min(max(1, max_pages), 20)), 1):
            self._notify(f"Faturamento P{pno} · bloco {i}")
            pg = self._get(f"/api_v2/faturamento/P{pno}")
            self._observe_page("faturamento", "page", pno, pg)
            if pg.http == 200 and pg.records:
                out.extend(pg.records)
                continue
            if pg.http in (200, 400, 404) and not pg.records:
                break
            raise ConnectorError(f"Faturamento: HTTP {pg.http} em P{pno}")
        return out

    def _faturamento_period_binary(self, start: date, end: date, max_pages: int, last: int) -> tuple[list[dict], dict]:
        """Fallback para períodos antigos: estratégia histórica do Analytics V7.8."""
        total_calls = 0

        def locate(target: date, after: bool) -> tuple[int, int]:
            lo, hi, calls = 1, max(1, int(last)), 0
            while lo < hi and calls < 30:
                mid = (lo + hi) // 2
                self._notify(f"Fallback histórico · localizando P{mid}")
                pg = self._get(f"/api_v2/faturamento/P{mid}")
                self._observe_page("faturamento", "page", mid, pg)
                calls += 1
                min_d, max_d = self._range("faturamento", pg.records, navigation=True)
                self._notify(
                    f"Fallback histórico · P{mid} · "
                    f"{min_d.isoformat() if min_d else '—'}→{max_d.isoformat() if max_d else '—'}"
                )
                if after:
                    if min_d is not None and min_d > target:
                        hi = mid
                    else:
                        lo = mid + 1
                else:
                    if max_d is None or max_d < target:
                        lo = mid + 1
                    else:
                        hi = mid
            return lo, calls

        start_page, c1 = locate(start, False)
        end_after, c2 = locate(end, True)
        total_calls += c1 + c2
        end_page = min(last, end_after + 2)
        start_page = max(1, start_page - 2)
        expected = max(0, end_page - start_page + 1)
        if expected > max(1, int(max_pages or 1)):
            raise ConnectorError(
                f"Faturamento: período antigo exige aproximadamente {expected} páginas; "
                f"aumente o limite (atual {max_pages})."
            )

        out: list[dict] = []
        for idx, pno in enumerate(range(start_page, end_page + 1), 1):
            self._notify(f"Fallback histórico · lendo P{pno} · {idx}/{max(1, expected)}")
            pg = self._get(f"/api_v2/faturamento/P{pno}")
            self._observe_page("faturamento", "page", pno, pg)
            total_calls += 1
            if pg.http != 200 or not pg.records:
                continue
            out.extend(self._filter_period("faturamento", pg.records, start.isoformat(), end.isoformat()))

        unique: list[dict] = []
        seen: set[str] = set()
        for row in out:
            key = str(deep_find(row, ("id", "id_faturamento", "id_nota", "nfe", "documento", "codigo_autenticacao_digital")) or repr(row))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)

        return unique, {
            "initialized": True,
            "last_valid": last,
            "first_invalid": last + 1,
            "last_http_requests": total_calls,
            "coverage_from": start.isoformat(),
            "coverage_to": end.isoformat(),
            "coverage_complete": True,
            "note": "Fallback histórico por busca binária do Analytics V7.8.",
        }

    def faturamento_period(self, start_date: str, end_date: str, max_pages: int = 100, last_valid: int | None = None, page_hint: dict | None = None, force_cached_range: bool = False) -> tuple[list[dict], dict]:
        """Faturamento por período otimizado para a rotina diária.

        Fluxo padrão:
        1. comprova a última página válida atual;
        2. parte dela e lê para trás;
        3. processa somente registros do intervalo informado;
        4. para assim que uma página inteira ficar anterior à data inicial.

        Para períodos muito antigos em relação à última emissão encontrada, usa a
        busca binária do Analytics V7.8 como fallback para não percorrer milhares
        de páginas uma a uma.
        """
        try:
            start = date.fromisoformat(start_date[:10])
            end = date.fromisoformat(end_date[:10])
        except Exception as e:
            raise ConnectorError("Período inválido para Faturamento.") from e
        if end < start:
            start, end = end, start

        max_pages = max(1, int(max_pages or 1))
        if page_hint and force_cached_range and page_hint.get("min_cursor") is not None and page_hint.get("max_cursor") is not None:
            low = max(1, int(page_hint["min_cursor"]) - 2)
            high = max(low, int(page_hint["max_cursor"]) + 2)
            expected = high - low + 1
            if expected > max_pages:
                raise ConnectorError(
                    f"Faturamento: o índice local aponta {expected} páginas para o período; "
                    f"aumente o limite (atual {max_pages})."
                )
            out: list[dict] = []
            calls = 0
            self._notify(f"Faturamento · usando índice local P{low}→P{high}")
            for idx, pno in enumerate(range(high, low - 1, -1), 1):
                self._notify(f"Faturamento · índice local · P{pno} · {idx}/{expected}")
                pg = self._get(f"/api_v2/faturamento/P{pno}")
                self._observe_page("faturamento", "page", pno, pg)
                calls += 1
                if pg.http == 200 and pg.records:
                    out.extend(self._filter_period("faturamento", pg.records, start.isoformat(), end.isoformat()))
            unique, seen = [], set()
            for row in out:
                key = str(deep_find(row, ("id", "id_faturamento", "id_nota", "nfe", "documento", "codigo_autenticacao_digital")) or repr(row))
                if key not in seen:
                    seen.add(key); unique.append(row)
            return unique, {
                "initialized": True, "last_http_requests": calls,
                "coverage_from": start.isoformat(), "coverage_to": end.isoformat(),
                "coverage_complete": True,
                "note": f"Período localizado usando índice local P{low}→P{high}; cursor incremental principal preservado.",
            }
        seed = max(1, int(last_valid or self.FATURAMENTO_SEED))
        total_calls = 0
        recovery_note = ""

        # 1) Comprova a página de partida. Cursores antigos podem apontar para
        # uma página que deixou de responder; nesse caso voltamos à âncora V7.8.
        self._notify(f"Fase 1/2 · validando última página conhecida P{seed}")
        anchor = seed
        try:
            pg_anchor = self._get(f"/api_v2/faturamento/P{anchor}")
            self._observe_page("faturamento", "page", anchor, pg_anchor)
            total_calls += 1
            anchor_ok = self._ok(pg_anchor)
        except ConnectorError:
            anchor_ok = False
            pg_anchor = None

        if not anchor_ok:
            anchor = self.FATURAMENTO_SEED
            recovery_note = f"Cursor P{seed} não confirmado; retomado pela âncora V7.8 P{anchor}. "
            self._notify(f"Fase 1/2 · cursor não confirmado; usando âncora P{anchor}")
            try:
                pg_anchor = self._get(f"/api_v2/faturamento/P{anchor}")
                self._observe_page("faturamento", "page", anchor, pg_anchor)
                total_calls += 1
            except ConnectorError as e:
                raise ConnectorError(
                    f"Faturamento: não foi possível validar nem o cursor P{seed} nem a âncora V7.8 P{anchor}. {e}"
                ) from e
            if not self._ok(pg_anchor):
                raise ConnectorError(f"Faturamento: âncora V7.8 P{anchor} não retornou registros válidos.")

        # 2) Avança somente enquanto P+1 for comprovadamente válida. A primeira
        # página vazia/inválida/timeout define a borda; ela jamais vira cursor.
        last = anchor
        last_page = pg_anchor
        edge_limit = max(30, min(200, max_pages * 2))
        for step in range(1, edge_limit + 1):
            candidate = last + 1
            self._notify(f"Fase 1/2 · procurando última página válida · testando P{candidate} · {step}/{edge_limit}")
            try:
                pg = self._get(f"/api_v2/faturamento/P{candidate}")
                self._observe_page("faturamento", "page", candidate, pg)
                total_calls += 1
            except ConnectorError:
                break
            if not self._ok(pg):
                break
            last = candidate
            last_page = pg

        latest_min, latest_max = self._range("faturamento", last_page.records, navigation=True)
        self._notify(
            f"Fase 1/2 · última página válida P{last} · "
            f"{latest_min.isoformat() if latest_min else '—'}→{latest_max.isoformat() if latest_max else '—'}"
        )

        # Períodos muito antigos são localizados pelo algoritmo binário do V7.8.
        if latest_max is not None and (latest_max - start).days > 120:
            self._notify(
                f"Período antigo ({start.isoformat()}→{end.isoformat()}); "
                "usando fallback histórico do V7.8"
            )
            records, meta = self._faturamento_period_binary(start, end, max_pages, last)
            meta["last_http_requests"] = int(meta.get("last_http_requests") or 0) + total_calls
            meta["note"] = recovery_note + str(meta.get("note") or "")
            return records, meta

        # 3) Rotina diária: da última válida para trás até ultrapassar a data inicial.
        out: list[dict] = []
        scanned = 0
        coverage_complete = False
        consecutive_failures = 0
        pno = last
        while pno >= 1 and scanned < max_pages:
            scanned += 1
            if pno == last:
                pg = last_page
            else:
                self._notify(f"Fase 2/2 · lendo faturamento de trás para frente · P{pno} · {scanned}/{max_pages}")
                try:
                    pg = self._get(f"/api_v2/faturamento/P{pno}")
                    self._observe_page("faturamento", "page", pno, pg)
                    total_calls += 1
                except ConnectorError as e:
                    consecutive_failures += 1
                    self._notify(f"Fase 2/2 · P{pno} sem resposta; seguindo para a anterior")
                    if consecutive_failures >= 3:
                        raise ConnectorError(
                            f"Faturamento: três páginas consecutivas sem resposta durante a leitura regressiva; última P{pno}. {e}"
                        ) from e
                    pno -= 1
                    continue

            if pg.http != 200 or not pg.records:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    raise ConnectorError(
                        f"Faturamento: três páginas consecutivas inválidas durante a leitura regressiva; última P{pno}."
                    )
                pno -= 1
                continue
            consecutive_failures = 0

            min_d, max_d = self._range("faturamento", pg.records, navigation=True)
            selected = self._filter_period("faturamento", pg.records, start.isoformat(), end.isoformat())
            if selected:
                out.extend(selected)
            self._notify(
                f"Fase 2/2 · P{pno} · "
                f"{min_d.isoformat() if min_d else '—'}→{max_d.isoformat() if max_d else '—'} · "
                f"{len(selected)} no período · {scanned}/{max_pages}"
            )

            # Página inteira já anterior ao início: cobertura concluída.
            if max_d is not None and max_d < start:
                coverage_complete = True
                break
            pno -= 1

        if not coverage_complete:
            # Se não conseguimos chegar antes da data inicial dentro do limite,
            # usa o fallback binário para evitar obrigar varredura página a página.
            self._notify(
                f"Fase 2/2 · limite de {max_pages} páginas atingido; "
                "acionando fallback histórico do V7.8"
            )
            records, meta = self._faturamento_period_binary(start, end, max_pages, last)
            meta["last_http_requests"] = int(meta.get("last_http_requests") or 0) + total_calls
            meta["note"] = recovery_note + "Varredura regressiva excedeu o limite; " + str(meta.get("note") or "")
            return records, meta

        # Deduplica preservando o payload completo.
        unique: list[dict] = []
        seen: set[str] = set()
        for row in out:
            key = str(deep_find(row, ("id", "id_faturamento", "id_nota", "nfe", "documento", "codigo_autenticacao_digital")) or repr(row))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)

        return unique, {
            "initialized": True,
            "last_valid": last,
            "first_invalid": last + 1,
            "last_http_requests": total_calls,
            "coverage_from": start.isoformat(),
            "coverage_to": end.isoformat(),
            "coverage_complete": True,
            "note": recovery_note + (
                f"Última página válida P{last}; leitura regressiva concluída em {scanned} página(s)."
            ),
        }

    def finance(self, kind: str, max_blocks: int = 20, start_pos: int | None = None, first_invalid: int | None = None) -> list[dict]:
        prefix = "/api_v2/baixareceita/pagina/" if kind == "receber" else "/api_v2/baixadespesa/pagina/"
        out: list[dict] = []
        if start_pos is None:
            seed = first_invalid or (self.RECEBER_FIRST_INVALID_SEED if kind == "receber" else None)
            end = self.discover_finance_end(prefix, max_calls=max(35, min(80, max_blocks)), seed=seed)
            pos = max(1, end - max(1, max_blocks) * 10)
        else:
            pos = max(1, int(start_pos or 1))
        for _ in range(max(1, max_blocks)):
            page = self._get(prefix + str(pos))
            self._observe_page(kind, "position", pos, page)
            if page.http in (400, 404) or not page.records:
                break
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"{kind}: HTTP {page.http} na posição {pos}")
            out.extend(page.records)
            pos += 10
        return out

    def finance_period(self, kind: str, start_date: str, end_date: str, max_blocks: int = 100,
                       first_invalid: int | None = None, page_hint: dict | None = None) -> tuple[list[dict], dict]:
        prefix = "/api_v2/baixareceita/pagina/" if kind == "receber" else "/api_v2/baixadespesa/pagina/"
        try:
            start = date.fromisoformat(start_date[:10]); end = date.fromisoformat(end_date[:10])
        except Exception as e:
            raise ConnectorError(f"Período inválido para {kind}.") from e
        if end < start:
            start, end = end, start
        seed = first_invalid or (self.RECEBER_FIRST_INVALID_SEED if kind == "receber" else None)
        edge = self.discover_finance_end(prefix, max_calls=max(35, min(80, max_blocks)), seed=seed)
        last_pos = max(1, edge - 1)
        locate_calls = 0

        def locate(target: date, after: bool) -> int:
            nonlocal locate_calls
            lo, hi = 1, last_pos
            while lo < hi and locate_calls < 70:
                mid = (lo + hi) // 2
                self._notify(f"Localizando {kind} · posição {mid}")
                page = self._get(prefix + str(mid))
                self._observe_page(kind, "position", mid, page)
                locate_calls += 1
                min_d, max_d = self._range(kind, page.records)
                if after:
                    if min_d is not None and min_d > target:
                        hi = mid
                    else:
                        lo = mid + 1
                else:
                    if max_d is None or max_d < target:
                        lo = mid + 1
                    else:
                        hi = mid
            return lo

        if page_hint and page_hint.get("min_cursor") is not None and page_hint.get("max_cursor") is not None:
            start_pos = max(1, (int(page_hint["min_cursor"]) // 10) * 10 - 20)
            cached_end = (int(page_hint["max_cursor"]) // 10) * 10 + 20
            end_pos = min(last_pos, max(cached_end, last_pos if page_hint.get("touches_recent") else cached_end))
            self._notify(f"{kind} · índice local {start_pos}→{end_pos} ({page_hint.get('count', 0)} bloco(s) conhecido(s))")
        else:
            start_pos = max(1, (locate(start, False) // 10) * 10 - 20)
            end_pos = min(last_pos, (locate(end, True) // 10) * 10 + 20)
        expected = ((end_pos - start_pos) // 10) + 1 if end_pos >= start_pos else 0
        if expected > max_blocks:
            raise ConnectorError(f"{kind}: período exige ~{expected} blocos; aumente o limite (atual {max_blocks}).")
        out: list[dict] = []
        calls = 0
        for pos in range(start_pos, end_pos + 1, 10):
            self._notify(f"Histórico {kind} · posição {pos} ({calls+1}/{max(1, expected)})")
            page = self._get(prefix + str(pos)); calls += 1
            self._observe_page(kind, "position", pos, page)
            if page.http != 200:
                raise ConnectorError(f"{kind}: HTTP {page.http} na posição {pos}")
            if not page.records:
                continue
            out.extend(self._filter_period(kind, page.records, start.isoformat(), end.isoformat()))
        return out, {"initialized": True, "first_invalid": edge, "last_valid": max(1, edge - 1),
                     "last_http_requests": calls + locate_calls, "coverage_from": start.isoformat(),
                     "coverage_to": end.isoformat(), "coverage_complete": True}

    def purchases(self, max_blocks: int = 20, start_offset: int | None = None) -> list[dict]:
        out: list[dict] = []
        if start_offset is None:
            last = self.discover_last_offset("/api/compra/confirmados/", step=10, max_calls=40)
            offset = max(0, last - 30)
        else:
            offset = max(0, int(start_offset or 0))
        for _ in range(max(1, max_blocks)):
            path = "/api/compra/confirmados/" + (str(offset) if offset else "")
            page = self._get(path)
            self._observe_page("compras", "offset", offset, page)
            if page.http in (400, 404) or not page.records:
                break
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"Compras: HTTP {page.http} no offset {offset}")
            out.extend(page.records)
            if len(page.records) < 10:
                break
            offset += 10
        return out

    def purchases_period(self, start_date: str, end_date: str, max_blocks: int = 100, last_offset: int | None = None, page_hint: dict | None = None, force_cached_range: bool = False) -> tuple[list[dict], dict]:
        """Varre compras recentes do último offset para trás e filtra pelo período."""
        try:
            start = date.fromisoformat(start_date[:10]); end = date.fromisoformat(end_date[:10])
        except Exception as e:
            raise ConnectorError("Período inválido para Compras.") from e
        if end < start:
            start, end = end, start

        if page_hint and force_cached_range and page_hint.get("min_cursor") is not None and page_hint.get("max_cursor") is not None:
            low = max(0, (int(page_hint["min_cursor"]) // 10) * 10 - 20)
            high = max(low, (int(page_hint["max_cursor"]) // 10) * 10 + 20)
            expected = ((high - low) // 10) + 1
            if expected > max_blocks:
                raise ConnectorError(f"Compras: índice local exige ~{expected} blocos; aumente o limite (atual {max_blocks}).")
            out: list[dict] = []
            calls = 0
            self._notify(f"Compras · usando índice local {low}→{high}")
            for offset in range(high, low - 1, -10):
                path = "/api/compra/confirmados/" + (str(offset) if offset else "")
                pg = self._get(path)
                self._observe_page("compras", "offset", offset, pg)
                calls += 1
                if pg.http == 200 and pg.records:
                    out.extend(self._filter_period("compras", pg.records, start.isoformat(), end.isoformat()))
            unique, seen = [], set()
            for row in out:
                key = str(deep_find(row, ("id", "id_compra", "codigo", "documento", "nfe")) or repr(row))
                if key not in seen:
                    seen.add(key); unique.append(row)
            return unique, {
                "initialized": True, "last_http_requests": calls,
                "coverage_from": start.isoformat(), "coverage_to": end.isoformat(),
                "coverage_complete": True,
                "note": f"Período localizado usando índice local {low}→{high}; cursor incremental principal preservado.",
            }

        if last_offset is not None and int(last_offset or 0) >= 0:
            # Revalida o cursor e, se necessário, avança para a borda atual.
            seed = max(0, int(last_offset or 0))
        else:
            seed = 0
        last = self.discover_last_offset("/api/compra/confirmados/", step=10, max_calls=45, seed=seed)
        out: list[dict] = []
        calls = 0
        reached_start = False
        offset = max(0, last)
        while offset >= 0 and calls < max(1, max_blocks):
            path = "/api/compra/confirmados/" + (str(offset) if offset else "")
            pg = self._get(path)
            self._observe_page("compras", "offset", offset, pg)
            calls += 1
            if pg.http != 200:
                if offset == 0:
                    break
                offset = max(0, offset - 10)
                continue
            min_d, max_d = self._range("compras", pg.records, navigation=True)
            selected = self._filter_period("compras", pg.records, start.isoformat(), end.isoformat())
            out.extend(selected)
            self._notify(
                f"Compras · offset {offset} · {min_d.isoformat() if min_d else '?'}→{max_d.isoformat() if max_d else '?'} · {len(selected)} no período"
            )
            if max_d is not None and max_d < start:
                reached_start = True
                break
            if offset == 0:
                reached_start = True
                break
            offset = max(0, offset - 10)

        if not reached_start and calls >= max(1, max_blocks):
            raise ConnectorError(
                f"Compras: limite de {max_blocks} blocos atingido antes de alcançar {start.isoformat()}. "
                "Aumente o limite de páginas de movimentos."
            )

        unique: list[dict] = []
        seen: set[str] = set()
        for row in out:
            key = str(deep_find(row, ("id", "id_compra", "codigo", "documento", "nfe")) or repr(row))
            if key in seen:
                continue
            seen.add(key); unique.append(row)
        return unique, {
            "initialized": True, "next_offset": last, "last_http_requests": calls,
            "coverage_from": start.isoformat(), "coverage_to": end.isoformat(),
            "coverage_complete": reached_start,
        }

    def expenses_period(self, start_date: str, end_date: str, max_days: int = 366) -> list[dict]:
        from datetime import timedelta
        try:
            start = date.fromisoformat(start_date); end = date.fromisoformat(end_date)
        except Exception as e:
            raise ConnectorError("Período de despesas inválido.") from e
        if end < start:
            start, end = end, start
        out: list[dict] = []
        cur = start; calls = 0
        while cur <= end and calls < max(1, max_days):
            path = "/api_v2/despesa/d" + cur.strftime("%d-%m-%Y")
            page = self._get(path); calls += 1
            if page.http == 404:
                cur += timedelta(days=1); continue
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"Despesas: HTTP {page.http} em {cur.isoformat()}")
            out.extend(page.records)
            cur += timedelta(days=1)
        if cur <= end:
            raise ConnectorError(f"Período de despesas excede o limite de {max_days} dias consultados.")
        return out

    def expenses(self, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        for p in range(1, max(1, max_pages) + 1):
            path = "/api_v2/despesa" if p == 1 else f"/api_v2/despesa/P{p}"
            page = self._get(path)
            if page.http in (400, 404) or not page.records:
                break
            if page.http < 200 or page.http >= 300:
                raise ConnectorError(f"Despesas: HTTP {page.http}")
            out.extend(page.records)
            if len(page.records) < 10:
                break
        return out

    def purchase_detail(self, external_id: str) -> dict | None:
        page = self._get("/api/compra/confirmado/" + str(external_id))
        if not (200 <= page.http < 300):
            return None
        # No endpoint de detalhe o maior array frequentemente é a lista de itens.
        # O V7.8 prioriza compra/compras/confirmados/data para obter o cabeçalho.
        headers = _extract_named_records(page.payload, ("compras", "compra", "confirmados", "data"))
        detail = dict(headers[0]) if headers else (_one_object_data(page.payload) or {})
        if not detail and page.records:
            detail = dict(page.records[0])
        if not detail:
            return None
        items = _find_named_object_list(page.payload, ("produtos", "itens", "items", "produtos_itens"))
        if items:
            detail["itens"] = items
        return detail


class NFStockClient:
    """Cliente de leitura baseado no adaptador do CompraSmart NFStock v1.20."""

    def __init__(self, settings: dict | None = None, progress: Callable[[str], None] | None = None, page_observer: Callable[[str, str, int, list[dict]], None] | None = None):
        settings = settings or {}
        self._progress = progress
        self.active = bool(settings.get("active", True))
        self.base = (settings.get("base_url") or os.getenv("NFSTOCK_BASE_URL") or "https://ms-exportacao-nfstock.pack.alterdata.com.br").rstrip("/")
        self.token = settings.get("token") if "token" in settings else (os.getenv("NFSTOCK_TOKEN") or "")
        crm = settings.get("crm") if "crm" in settings else (os.getenv("NFSTOCK_CRM") or "")
        self.crm = str(crm or "").zfill(6) if crm else ""
        cnpj = settings.get("cnpj") if "cnpj" in settings else (os.getenv("COMPANY_CNPJ") or "")
        self.cnpj = "".join(ch for ch in str(cnpj or "") if ch.isdigit())
        try:
            raw_page = settings.get("page_size") if "page_size" in settings else (os.getenv("NFSTOCK_PAGE_SIZE") or 25)
            self.page_size = max(1, min(100, int(raw_page or 25)))
        except Exception:
            self.page_size = 25

    @property
    def configured(self) -> bool:
        return bool(self.active and self.base and self.token and self.crm and self.cnpj)

    def _headers(self) -> dict:
        # Não enviar Accept: application/json: versões da MS-Exportação podem responder 406.
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _docs(payload: Any) -> list[dict]:
        return extract_records(payload, preferred=("documentos", "nfe", "nfes", "notas"))

    def list_period(self, start_date: str, end_date: str, max_pages: int = 100) -> list[dict]:
        if not self.configured:
            raise ConnectorError("Configure NFSTOCK_TOKEN, NFSTOCK_CRM e COMPANY_CNPJ.")
        url = f"{self.base}/api/v1/{self.crm}/{self.cnpj}/nfe"
        out: list[dict] = []
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            for page in range(1, max(1, max_pages) + 1):
                params = {"DataInicial": start_date, "DataFinal": end_date, "Tamanho": self.page_size, "Pagina": page}
                r = client.get(url, params=params, headers=self._headers())
                if r.status_code >= 400:
                    raise ConnectorError(f"NFStock HTTP {r.status_code}: {r.text[:400]}")
                try:
                    payload = r.json()
                except Exception as e:
                    raise ConnectorError(f"NFStock retornou JSON inválido: {r.text[:250]}") from e
                docs = self._docs(payload)
                if not docs:
                    break
                out.extend(docs)
                if len(docs) < self.page_size:
                    break
        return out

    def document_by_key(self, key: str, xml: bool = True) -> Any:
        if not self.configured:
            raise ConnectorError("NFStock não configurado.")
        url = f"{self.base}/api/v1/{self.crm}/{self.cnpj}/documentos/{key}/chave"
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            r = client.get(url, params={"Xml": str(xml).lower()}, headers=self._headers())
            if r.status_code >= 400:
                raise ConnectorError(f"NFStock HTTP {r.status_code}: {r.text[:400]}")
            ctype = (r.headers.get("content-type") or "").lower()
            if "xml" in ctype or r.text.lstrip().startswith("<"):
                return {"xml": r.text}
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}

    def document_by_nsu(self, nsu: str, xml: bool = True) -> Any:
        if not self.configured:
            raise ConnectorError("NFStock não configurado.")
        url = f"{self.base}/api/v1/{self.crm}/{self.cnpj}/documentos/{int(nsu)}"
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            r = client.get(url, params={"Xml": str(xml).lower()}, headers=self._headers())
            if r.status_code >= 400:
                raise ConnectorError(f"NFStock HTTP {r.status_code}: {r.text[:400]}")
            ctype = (r.headers.get("content-type") or "").lower()
            if "xml" in ctype or r.text.lstrip().startswith("<"):
                return {"xml": r.text}
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}

    @staticmethod
    def extract_xml(obj: Any) -> str:
        if isinstance(obj, str):
            return obj if obj.lstrip().startswith("<") else ""
        if not isinstance(obj, dict):
            return ""
        for name in ("xml", "Xml", "conteudoXml", "conteudo_xml", "arquivoXml"):
            raw = obj.get(name)
            if not raw:
                continue
            if isinstance(raw, str) and raw.lstrip().startswith("<"):
                return raw
            if isinstance(raw, str):
                try:
                    decoded = base64.b64decode(raw).decode("utf-8-sig")
                    if decoded.lstrip().startswith("<"):
                        return decoded
                except Exception:
                    pass
        for v in obj.values():
            if isinstance(v, dict):
                found = NFStockClient.extract_xml(v)
                if found:
                    return found
        return ""

    @staticmethod
    def download_url(obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        for name in ("url_download", "urlDownload", "UrlDownload", "download_url", "downloadUrl", "url"):
            v = obj.get(name)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        for v in obj.values():
            if isinstance(v, dict):
                found = NFStockClient.download_url(v)
                if found:
                    return found
        return ""

    def xml_for(self, meta: dict, full: Any = None) -> str:
        xml = self.extract_xml(full) or self.extract_xml(meta)
        if xml:
            return xml
        url = self.download_url(full) or self.download_url(meta)
        if not url:
            return ""
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code in (401, 403):
                r = client.get(url, headers=self._headers())
            if r.status_code >= 400:
                raise ConnectorError(f"Download XML HTTP {r.status_code}: {r.text[:250]}")
            content = r.content
        if content[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    for name in z.namelist():
                        if name.lower().endswith(".xml"):
                            return z.read(name).decode("utf-8-sig", errors="replace")
            except Exception:
                return ""
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                txt = content.decode(enc)
                if txt.lstrip().startswith("<"):
                    return txt
            except Exception:
                pass
        return ""

    def test(self) -> dict:
        # Consulta curta do período atual sem exigir que existam documentos.
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=1)
        docs = self.list_period(start.isoformat(), end.isoformat(), max_pages=1)
        return {"ok": True, "records": len(docs), "period": f"{start}..{end}"}
