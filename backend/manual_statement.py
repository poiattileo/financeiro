"""Parse personal bank statement exports without keeping the uploaded file."""
import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation


def _key(value):
    value = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in value if not unicodedata.combining(c)))


def _date(value):
    value = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    if re.match(r"^\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
    raise ValueError("Data inválida no extrato")


def _amount(value):
    raw = re.sub(r"[^0-9,.-]", "", str(value or "").strip())
    if not raw:
        raise ValueError("Valor vazio no extrato")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Valor inválido no extrato") from exc
    if not number.is_finite() or abs(number) > Decimal("9999999999999.99"):
        raise ValueError("Valor fora do limite no extrato")
    return number.quantize(Decimal("0.01"))


def _field(row, *aliases):
    normalized = {_key(k): v for k, v in row.items() if k is not None}
    for alias in aliases:
        if _key(alias) in normalized:
            return normalized[_key(alias)]
    return None


def _csv_rows(raw):
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    first = content[:4096]
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not rows.fieldnames:
        raise ValueError("CSV sem cabeçalho")
    result = []
    for row in rows:
        if not any(str(v or "").strip() for v in row.values()):
            continue
        day = _field(row, "data", "date", "data lançamento", "data transação", "data transacao", "data movimento")
        description = _field(row, "descricao", "descrição", "description", "historico", "histórico", "detalhes", "titulo")
        value = _field(row, "valor", "amount", "valor r$", "valor da transação", "valor da transacao")
        debit = _field(row, "debito", "débito", "saida", "saída")
        credit = _field(row, "credito", "crédito", "entrada")
        if day is None or description is None or (value is None and debit is None and credit is None):
            raise ValueError("CSV precisa de colunas Data, Descrição e Valor (ou Débito/Crédito)")
        if value is None:
            value = -abs(_amount(debit)) if str(debit or "").strip() else abs(_amount(credit))
        else:
            value = _amount(value)
        kind = _key(_field(row, "tipo", "type", "natureza") or "")
        if kind in ("debito", "saida", "pagamento"):
            value = -abs(value)
        elif kind in ("credito", "entrada", "recebimento"):
            value = abs(value)
        result.append((_date(day), str(description).strip()[:500], value, None))
    return result


def _tag(block, tag):
    match = re.search(r"<" + tag + r">\s*([^<\r\n]+)", block, re.I)
    return match.group(1).strip() if match else ""


def _ofx_rows(raw):
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    result = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", content, re.I | re.S):
        day = _date(_tag(block, "DTPOSTED"))
        value = _amount(_tag(block, "TRNAMT"))
        description = (_tag(block, "MEMO") or _tag(block, "NAME") or "Movimentação").strip()[:500]
        result.append((day, description, value, _tag(block, "FITID") or None))
    return result


def parse_statement(raw, filename, month, namespace=""):
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Arquivo acima de 5 MB")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise ValueError("Escolha um mês válido")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("csv", "ofx"):
        raise ValueError("Envie um arquivo CSV ou OFX")
    rows = _csv_rows(raw) if ext == "csv" else _ofx_rows(raw)
    if not rows:
        raise ValueError("Nenhuma movimentação encontrada no arquivo")
    if len(rows) > 5000:
        raise ValueError("Extrato acima de 5.000 movimentações")
    selected = [row for row in rows if row[0].startswith(month)]
    if not selected:
        raise ValueError("O extrato não contém movimentações do mês selecionado")
    seen = Counter()
    result = []
    for day, description, amount, fitid in selected:
        if not description or amount == 0:
            continue
        basis = fitid or f"{day}|{description}|{amount}"
        seen[basis] += 1
        digest = hashlib.sha256(f"{namespace}|{basis}|{seen[basis] if not fitid else 0}".encode()).hexdigest()[:48]
        result.append({"id": "manual:" + digest, "date": day,
                       "description": description, "amount": amount,
                       "direction": "DEBIT" if amount < 0 else "CREDIT"})
    return result
