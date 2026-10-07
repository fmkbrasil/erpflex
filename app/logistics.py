from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import quote_plus


def norm(value) -> str:
    txt = "" if value is None else str(value).strip().lower()
    txt = unicodedata.normalize("NFKD", txt)
    return "".join(c for c in txt if not unicodedata.combining(c))


def digits(value) -> str:
    return re.sub(r"\D", "", "" if value is None else str(value))


def cep_num(cep):
    dig = digits(cep)
    if len(dig) >= 8:
        return int(dig[:8])
    if len(dig) >= 5:
        return int(dig[:5].ljust(8, "0"))
    return None


def classificar_regiao(municipio, bairro="", cep="", canal="") -> str:
    """Classificador operacional trazido do Roteirizador V46.5.20.

    Não consulta serviços externos e pode ser aplicado tanto em registros vindos
    do ERPFlex quanto na importação Excel de contingência.
    """
    mun = norm(municipio)
    bai = norm(bairro)
    can = norm(canal)
    cepn = cep_num(cep)

    if "zona leste" in can:
        return "Zona Leste"
    if "zona sul" in can:
        return "Zona Sul"
    if "zona norte" in can:
        return "Zona Norte"
    if "zona oeste" in can:
        return "Zona Oeste"
    if "centro" in can and "lebanon centro" not in can:
        return "Centro"

    abc = {"santo andre", "sao bernardo do campo", "sao caetano do sul", "diadema", "maua", "ribeirao pires", "rio grande da serra"}
    oeste_grande_sp = {"osasco", "barueri", "carapicuiba", "jandira", "itapevi", "santana de parnaiba", "pirapora do bom jesus"}
    alto_tiete = {"mogi das cruzes", "suzano", "poa", "itaquaquecetuba", "aruja", "ferraz de vasconcelos", "biritiba mirim", "guararema", "salesopolis"}
    litoral = {"santos", "sao vicente", "guaruja", "praia grande", "cubatao", "bertioga", "mongagua", "itanhaem", "peruibe", "sao sebastiao", "ilhabela", "caraguatatuba", "ubatuba", "cananeia", "iguape", "ilha comprida"}

    if mun in abc:
        return "ABC Paulista"
    if mun == "guarulhos":
        return "Guarulhos"
    if mun in oeste_grande_sp:
        return "Osasco / Barueri"
    if mun in alto_tiete:
        return "Alto Tietê"
    if mun in litoral:
        return "Litoral SP"

    if mun in {"sao paulo", "s. paulo", "sp"}:
        if cepn is not None:
            prefixo = cepn // 100000
            if 10 <= prefixo <= 15:
                return "Centro"
            if 20 <= prefixo <= 29:
                return "Zona Norte"
            if 30 <= prefixo <= 39 or 80 <= prefixo <= 84:
                return "Zona Leste"
            if 40 <= prefixo <= 49:
                return "Zona Sul"
            if 50 <= prefixo <= 59:
                return "Zona Oeste"

        centro = {"se", "republica", "bela vista", "liberdade", "cambuci", "consolacao", "santa cecilia", "bom retiro", "bras"}
        norte = {"santana", "tucuruvi", "mandaqui", "casa verde", "limao", "freguesia do o", "brasilandia", "jacana", "tremembe", "vila maria", "vila guilherme"}
        leste = {"tatuape", "mooca", "belem", "penha", "itaquera", "sao mateus", "sao miguel paulista", "itaim paulista", "guaianases", "aricanduva", "vila prudente", "sapopemba", "cidade tiradentes", "catumbi"}
        sul = {"moema", "vila mariana", "saude", "jabaquara", "santo amaro", "campo belo", "brooklin", "vila olimpia", "itaim bibi", "interlagos", "socorro", "cidade ademar", "grajau", "parelheiros", "capao redondo", "campo limpo", "vila andrade", "jardim sao luis"}
        oeste = {"pinheiros", "perdizes", "lapa", "alto de pinheiros", "vila leopoldina", "butanta", "morumbi", "jaguare", "rio pequeno", "raposo tavares", "vila sonia", "barra funda"}
        for names, region in ((centro, "Centro"), (norte, "Zona Norte"), (leste, "Zona Leste"), (sul, "Zona Sul"), (oeste, "Zona Oeste")):
            if any(name in bai for name in names):
                return region
        return "A Classificar"

    if cepn is not None and 1000000 <= cepn <= 19999999:
        return "Interior SP"
    return "Outras Regiões" if mun or cepn else "A Classificar"


def regiao_especial(transportadora="", transportadora2="") -> str | None:
    t1 = norm(transportadora)
    t2 = norm(transportadora2)
    both = f"{t1} | {t2}"
    if t2 == "destinatario coleta":
        return "COLETA"
    if "lebanon centro" in both or "lebanon onibus" in both:
        return "Centro"
    return None


def alerta_endereco(transportadora="", transportadora2="") -> str | None:
    both = f"{norm(transportadora)} | {norm(transportadora2)}"
    if "lebanon onibus" in both or "onibus" in both:
        return "VERIFICAR ENDEREÇO NA NOTA FISCAL"
    return None


def cliente_exibicao(name: str | None, trade_name: str | None) -> str:
    razao = re.sub(r"\s+", " ", str(name or "").strip())
    fantasia = re.sub(r"\s+", " ", str(trade_name or "").strip())
    if razao and fantasia:
        a = re.sub(r"\W+", "", razao).casefold()
        b = re.sub(r"\W+", "", fantasia).casefold()
        return razao if a == b else f"{razao} — {fantasia}"
    return razao or fantasia or "—"


def raw_channel(payload_json: str | None) -> str:
    if not payload_json:
        return ""
    try:
        obj = json.loads(payload_json)
    except Exception:
        return ""
    keys = {"canal", "canal_venda", "canalvenda"}
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if norm(k).replace(" ", "_") in keys and v not in (None, ""):
                    return str(v).strip()
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def endereco_busca(address: str | None, district: str | None, city: str | None, state: str | None, zip_code: str | None) -> str:
    parts = [address, district, city, state, zip_code]
    return ", ".join(str(x).strip() for x in parts if str(x or "").strip())


def maps_url(query: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}" if query else "#"


def waze_url(query: str) -> str:
    return f"https://waze.com/ul?q={quote_plus(query)}&navigate=yes" if query else "#"
