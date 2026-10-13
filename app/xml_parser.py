import re
import xml.etree.ElementTree as ET
from datetime import datetime


def digits(v):
    return re.sub(r'\D', '', v or '')


def txt(node, path):
    el = node.find(path)
    return (el.text or '').strip() if el is not None and el.text else ''


def fnum(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def local(tag):
    return tag.split('}', 1)[-1]


def child(parent, name):
    if parent is None:
        return None
    for c in parent.iter():
        if local(c.tag) == name:
            return c
    return None


def ctext(parent, name):
    c = child(parent, name)
    return (c.text or '').strip() if c is not None and c.text else ''


PAYMENT_METHODS = {
    '01': 'Dinheiro', '02': 'Cheque', '03': 'Cartão de crédito', '04': 'Cartão de débito',
    '05': 'Crédito loja', '10': 'Vale alimentação', '11': 'Vale refeição', '12': 'Vale presente',
    '13': 'Vale combustível', '14': 'Duplicata mercantil', '15': 'Boleto bancário',
    '16': 'Depósito bancário', '17': 'PIX', '18': 'Transferência/carteira digital',
    '19': 'Fidelidade/cashback', '90': 'Sem pagamento', '99': 'Outros'
}


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except Exception:
        return None


def _payment_info(inf, issue_date):
    """Extrai prazo real (duplicatas/vencimentos) e forma de pagamento da NF-e."""
    issue = _parse_iso_date(issue_date)
    due_days = []
    due_dates = []

    cobr = child(inf, 'cobr')
    if cobr is not None:
        for dup in [x for x in cobr.iter() if local(x.tag) == 'dup']:
            venc = ctext(dup, 'dVenc')
            if venc:
                due_dates.append(venc)
                d = _parse_iso_date(venc)
                if issue and d:
                    due_days.append((d - issue).days)

    methods = []
    pay = child(inf, 'pag')
    installment_flags = []
    if pay is not None:
        for det in [x for x in pay.iter() if local(x.tag) == 'detPag']:
            code = ctext(det, 'tPag')
            if code:
                label = PAYMENT_METHODS.get(code, f'Forma {code}')
                if label not in methods:
                    methods.append(label)
            ind = ctext(det, 'indPag')
            if ind:
                installment_flags.append(ind)

    payment_terms = ''
    positive_days = [x for x in due_days if x >= 0]
    if positive_days:
        unique = []
        for x in positive_days:
            if x not in unique:
                unique.append(x)
        if len(unique) == 1 and unique[0] == 0:
            payment_terms = 'À vista'
        else:
            payment_terms = '/'.join(str(x) for x in unique) + ' dias'
    elif due_dates:
        payment_terms = 'Venc. ' + ' / '.join(due_dates)
    elif '0' in installment_flags:
        payment_terms = 'À vista'
    elif '1' in installment_flags:
        payment_terms = 'A prazo'
    elif methods and all(m == 'Sem pagamento' for m in methods):
        payment_terms = 'Sem pagamento'

    return payment_terms, ' + '.join(methods), due_dates


def parse_nfe(xml_text: str, company_cnpj: str):
    root = ET.fromstring(xml_text)
    inf = next((x for x in root.iter() if local(x.tag) == 'infNFe'), None)
    if inf is None:
        raise ValueError('XML não contém infNFe')
    ide = child(inf, 'ide')
    emit = child(inf, 'emit')
    dest = child(inf, 'dest')
    total_node = child(inf, 'ICMSTot')
    issuer_cnpj = digits(ctext(emit, 'CNPJ') or ctext(emit, 'CPF'))
    recipient_cnpj = digits(ctext(dest, 'CNPJ') or ctext(dest, 'CPF'))
    company = digits(company_cnpj)
    if recipient_cnpj and recipient_cnpj == company:
        direction = 'ENTRADA'
    elif issuer_cnpj and issuer_cnpj == company:
        direction = 'SAIDA'
    else:
        direction = 'INDEFINIDA'
    access_key = (inf.attrib.get('Id') or '').replace('NFe', '')
    if not access_key:
        prot = child(root, 'protNFe')
        access_key = ctext(prot, 'chNFe')

    issue_date = ctext(ide, 'dhEmi') or ctext(ide, 'dEmi')
    payment_terms, payment_method, payment_due_dates = _payment_info(inf, issue_date)

    items = []
    for det in [x for x in inf if local(x.tag) == 'det']:
        prod = child(det, 'prod')
        imposto = child(det, 'imposto')
        icms = child(imposto, 'ICMS')
        ipi = child(imposto, 'IPI')
        pis = child(imposto, 'PIS')
        cof = child(imposto, 'COFINS')

        def first_tax_value(node, names):
            if node is None:
                return 0
            for n in names:
                v = ctext(node, n)
                if v:
                    return fnum(v)
            return 0

        items.append({
            'product_code': ctext(prod, 'cProd'), 'ean': ctext(prod, 'cEAN'),
            'description': ctext(prod, 'xProd'), 'ncm': ctext(prod, 'NCM'), 'cfop': ctext(prod, 'CFOP'), 'unit': ctext(prod, 'uCom'),
            'qty': fnum(ctext(prod, 'qCom')), 'unit_price': fnum(ctext(prod, 'vUnCom')), 'total': fnum(ctext(prod, 'vProd')),
            'icms': first_tax_value(icms, ['vICMS']), 'icms_st': first_tax_value(icms, ['vICMSST', 'vST']),
            'ipi': first_tax_value(ipi, ['vIPI']), 'pis': first_tax_value(pis, ['vPIS']), 'cofins': first_tax_value(cof, ['vCOFINS'])
        })
    return {
        'access_key': access_key, 'number': ctext(ide, 'nNF'), 'series': ctext(ide, 'serie'), 'model': ctext(ide, 'mod'),
        'issuer_cnpj': issuer_cnpj, 'issuer_name': ctext(emit, 'xNome'),
        'recipient_cnpj': recipient_cnpj, 'recipient_name': ctext(dest, 'xNome'),
        'issue_date': issue_date, 'operation_nature': ctext(ide, 'natOp'),
        'total': fnum(ctext(total_node, 'vNF')), 'direction': direction,
        'payment_terms': payment_terms, 'payment_method': payment_method,
        'payment_due_dates': payment_due_dates, 'items': items
    }
