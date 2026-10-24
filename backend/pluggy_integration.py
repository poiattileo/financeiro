"""Pluggy sync, matching and FastAPI endpoints for the existing finance app."""
import json
import logging
import os
import re
import unicodedata
import hmac
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import pluggy_client as pluggy
from manual_statement import parse_statement

log = logging.getLogger(__name__)


def norm(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def name_matches(expected, actual):
    if not expected or not actual:
        return False
    if norm(expected) == norm(actual):
        return True
    def words(value):
        plain = unicodedata.normalize("NFKD", value)
        plain = "".join(c for c in plain if not unicodedata.combining(c))
        return re.findall(r"[a-z0-9]+", plain.lower())
    expected_words = words(expected)
    actual_words = words(actual)
    return len(norm(expected)) >= 5 and actual_words[:len(expected_words)] == expected_words


def pix_recipient(description, payment_method, direction):
    if str(payment_method or "").upper() != "PIX" or direction != "DEBIT":
        return None
    match = re.match(
        r"^\s*(?:pix\s+enviado(?:\s+para)?\s+|transfer[êe]ncia\s+enviada(?:\s+pelo\s+pix)?\s*\|\s*)(.+?)\s*$",
        description or "", re.I)
    return match.group(1).strip()[:200] if match else None


def only_digits(value):
    return re.sub(r"\D", "", value or "")


def local_date(value):
    if not value:
        raise ValueError("Transação sem data")
    # Pluggy's `date` is the posted calendar date; preserve it for monthly budgets.
    return date.fromisoformat(value[:10]).isoformat()


def expected_amount(cur, user_id, gasto_id, mes_ref):
    cur.execute("SELECT mes_inicio FROM usuarios WHERE id=%s", (user_id,))
    user = cur.fetchone()
    base = (user or {}).get("mes_inicio") or ""
    if not re.fullmatch(r"\d{4}-\d{2}", base):
        return Decimal("0")
    idx = (int(mes_ref[:4]) - int(base[:4])) * 12 + int(mes_ref[5:]) - int(base[5:])
    cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (gasto_id, idx))
    row = cur.fetchone()
    return abs(Decimal(str((row or {}).get("valor") or 0)))


def auto_match(cur, user_id, transaction_id):
    cur.execute("""SELECT t.*, a.type AS account_type FROM pluggy_transactions t
        JOIN pluggy_accounts a ON a.id=t.account_id WHERE t.id=%s AND t.user_id=%s""",
        (transaction_id, user_id))
    txn = cur.fetchone()
    if not txn or txn["auto_ignored"] or txn["account_type"] != "BANK" or txn["direction"] != "DEBIT" or txn["status"] != "POSTED":
        return
    recipient_name = txn["recipient_name"] or pix_recipient(
        txn["description"], txn["payment_method"], txn["direction"])
    if not recipient_name and not txn["recipient_document"]:
        return
    mes = str(txn["transaction_date"])[:7]
    cur.execute("SELECT id FROM pluggy_matches WHERE transaction_id=%s", (transaction_id,))
    if cur.fetchone():
        return
    cur.execute("""SELECT r.*, g.nome FROM pluggy_rules r JOIN gastos g ON g.id=r.gasto_id
        WHERE r.user_id=%s AND g.user_id=%s""", (user_id, user_id))
    candidates = []
    for rule in cur.fetchall():
        name_ok = name_matches(rule["recipient_name"], recipient_name)
        doc_ok = bool(rule["recipient_document"] and only_digits(rule["recipient_document"]) == only_digits(txn["recipient_document"]))
        if not (name_ok or doc_ok):
            continue
        expected = expected_amount(cur, user_id, rule["gasto_id"], mes)
        if expected <= 0:
            continue
        cur.execute("""SELECT COALESCE(SUM(ABS(t.amount)),0) AS paid FROM pluggy_matches m
            JOIN pluggy_transactions t ON t.id=m.transaction_id
            WHERE m.gasto_id=%s AND m.mes_ref=%s AND m.user_id=%s""",
            (rule["gasto_id"], mes, user_id))
        paid = Decimal(str(cur.fetchone()["paid"]))
        incoming = abs(Decimal(str(txn["amount"])))
        tolerance = Decimal(str(rule["tolerance"]))
        # Exact/full installment matches can be applied safely; partial payments are linked manually.
        if abs(expected - paid - incoming) <= tolerance:
            candidates.append(rule["gasto_id"])
    if len(candidates) == 1:
        cur.execute("""INSERT IGNORE INTO pluggy_matches
            (user_id,gasto_id,mes_ref,transaction_id,source) VALUES (%s,%s,%s,%s,'auto')""",
            (user_id, candidates[0], mes, transaction_id))
        cur.execute("""UPDATE pluggy_transactions t JOIN gastos g ON g.id=%s
            SET t.user_category=COALESCE(t.user_category,g.cat)
            WHERE t.id=%s AND t.user_id=%s""", (candidates[0], transaction_id, user_id))


def sync_card_bills(cur, account, item_id, user_id, item_label):
    """Keep bill amounts by due month; preserve any value edited by the user."""
    account_id = account["id"]
    cur.execute("SELECT gasto_id FROM pluggy_bill_gastos WHERE account_id=%s AND user_id=%s",
                (account_id, user_id))
    mapping = cur.fetchone()
    if mapping:
        gasto_id = mapping["gasto_id"]
    else:
        name = "Fatura " + (item_label or account.get("name") or "cartão")
        cur.execute("INSERT INTO gastos (user_id,nome,cat) VALUES (%s,%s,'cartao')",
                    (user_id, name[:100]))
        gasto_id = cur.lastrowid
        cur.execute("INSERT INTO pluggy_bill_gastos (account_id,gasto_id,user_id) VALUES (%s,%s,%s)",
                    (account_id, gasto_id, user_id))
    cur.execute("SELECT mes_inicio FROM usuarios WHERE id=%s", (user_id,))
    base = (cur.fetchone() or {}).get("mes_inicio") or ""
    page = 1
    while True:
        data = pluggy.request("GET", "/bills", params={"accountId": account_id,
                                                  "page": page, "pageSize": 100})
        for bill in data.get("results", []):
            try:
                due = local_date(bill.get("dueDate"))
                amount = Decimal(str(bill.get("totalAmount") or 0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                paid = sum((Decimal(str(p.get("amount") or 0)) for p in bill.get("payments") or []),
                           Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except (ValueError, ArithmeticError):
                continue
            cur.execute("SELECT total_amount FROM pluggy_card_bills WHERE id=%s AND user_id=%s",
                        (bill["id"], user_id))
            previous = cur.fetchone()
            cur.execute("""INSERT INTO pluggy_card_bills
                (id,account_id,item_id,user_id,due_date,total_amount,paid_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE due_date=VALUES(due_date),
                total_amount=VALUES(total_amount),paid_amount=VALUES(paid_amount)""",
                (bill["id"], account_id, item_id, user_id, due, amount, paid))
            if amount <= 0 or not re.fullmatch(r"\d{4}-\d{2}", base):
                continue
            idx = (int(due[:4]) - int(base[:4])) * 12 + int(due[5:7]) - int(base[5:7])
            if not 0 <= idx < 48:
                continue
            cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s",
                        (gasto_id, idx))
            current = cur.fetchone()
            if not current or (previous and Decimal(str(current["valor"])) == -Decimal(str(previous["total_amount"]))):
                cur.execute("""INSERT INTO gasto_valores (gasto_id,idx,valor) VALUES (%s,%s,%s)
                    ON DUPLICATE KEY UPDATE valor=VALUES(valor)""", (gasto_id, idx, -amount))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1


def sync_item(get_db, item_id, user_id, *, allow_unlinked=False):
    item = pluggy.request("GET", "/items/" + item_id)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT user_id FROM pluggy_items WHERE id=%s", (item_id,))
        owner = cur.fetchone()
        if owner and owner["user_id"] != user_id:
            raise ValueError("Esta conexão já pertence a outro usuário")
        external_id = item.get("clientUserId")
        if external_id != f"financas:{user_id}" and not (allow_unlinked or owner):
            raise ValueError("Esta conexão não pertence ao usuário")
        connector = item.get("connector") or {}
        name = connector.get("name", "") if isinstance(connector, dict) else ""
        cur.execute("""INSERT INTO pluggy_items (id,user_id,connector_name,status,source)
            VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
            connector_name=VALUES(connector_name), status=VALUES(status)""",
            (item_id, user_id, name, item.get("status", "CONNECTED"),
             "dashboard" if allow_unlinked and external_id != f"financas:{user_id}" else "widget"))
        cur.execute("SELECT display_name FROM pluggy_items WHERE id=%s AND user_id=%s",
                    (item_id, user_id))
        item_label = (cur.fetchone() or {}).get("display_name")
        page = 1
        while True:
            account_page = pluggy.request("GET", "/accounts", params={"itemId": item_id, "page": page})
            for account in account_page.get("results", []):
                account_id = account["id"]
                cur.execute("""INSERT INTO pluggy_accounts
                    (id,item_id,user_id,name,type,balance,currency_code)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                    name=VALUES(name),type=VALUES(type),balance=VALUES(balance),
                    currency_code=VALUES(currency_code)""",
                    (account_id, item_id, user_id, account.get("name") or "Conta",
                     account.get("type") or "BANK", account.get("balance"),
                     account.get("currencyCode") or "BRL"))
                for txn in pluggy.list_transactions(account_id):
                    payment = txn.get("paymentData") or {}
                    receiver = payment.get("receiver") or {}
                    payment_method = payment.get("paymentMethod") or txn.get("operationType")
                    recipient_name = receiver.get("name") or pix_recipient(
                        txn.get("description"), payment_method, txn.get("type"))
                    doc = receiver.get("documentNumber") or {}
                    doc = doc.get("value", "") if isinstance(doc, dict) else str(doc)
                    try:
                        day = local_date(txn.get("date"))
                    except ValueError:
                        log.warning("Pluggy transaction %s has invalid date", txn.get("id"))
                        continue
                    cur.execute("""INSERT INTO pluggy_transactions
                        (id,item_id,account_id,user_id,transaction_date,description,amount,
                         direction,status,payment_method,recipient_name,recipient_document,
                         provider_id,operation_type)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE transaction_date=VALUES(transaction_date),
                        description=VALUES(description),amount=VALUES(amount),
                        direction=VALUES(direction),status=VALUES(status),
                        payment_method=VALUES(payment_method),recipient_name=VALUES(recipient_name),
                        recipient_document=VALUES(recipient_document),provider_id=VALUES(provider_id),
                        operation_type=VALUES(operation_type)""",
                        (txn["id"], item_id, account_id, user_id, day,
                         (txn.get("description") or "")[:500], txn.get("amount") or 0,
                         txn.get("type") or "", txn.get("status") or "",
                         payment_method,
                         str(recipient_name)[:200] if recipient_name else None,
                         doc, txn.get("providerId"),
                         txn.get("operationType")))
                    if txn.get("status") != "POSTED":
                        cur.execute("DELETE FROM pluggy_matches WHERE transaction_id=%s AND user_id=%s",
                                    (txn["id"], user_id))
                    auto_match(cur, user_id, txn["id"])
                if account.get("type") == "CREDIT":
                    try:
                        sync_card_bills(cur, account, item_id, user_id, item_label)
                    except pluggy.PluggyError:
                        log.warning("Bills unavailable for account %s", account_id)
            if page >= int(account_page.get("totalPages") or 1):
                break
            page += 1
        cur.execute("UPDATE pluggy_items SET last_sync=UTC_TIMESTAMP() WHERE id=%s AND user_id=%s",
                    (item_id, user_id))
    finally:
        cur.close()
        db.close()


class RuleInput(BaseModel):
    gasto_id: int
    recipient_name: str = Field(min_length=2, max_length=200)
    recipient_document: str = Field(default="", max_length=40)
    tolerance: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class MatchInput(BaseModel):
    gasto_id: int
    transaction_id: str


class ImportInput(BaseModel):
    item_id: str


class CategoryInput(BaseModel):
    category: str = Field(max_length=50)


class ManualEntryInput(BaseModel):
    transaction_date: date
    description: str = Field(min_length=2, max_length=500)
    amount: Decimal = Field(gt=0, le=Decimal("9999999999999.99"))
    direction: str


class ItemLabelInput(BaseModel):
    display_name: str = Field(min_length=2, max_length=160)


def ensure_manual_neon(cur, user_id):
    item_id = f"manual:neon:{user_id}"
    account_id = f"{item_id}:bank"
    cur.execute("""INSERT INTO pluggy_items
        (id,user_id,connector_name,status,source,last_sync)
        VALUES (%s,%s,'Neon (manual)','MANUAL','manual',NULL)
        ON DUPLICATE KEY UPDATE connector_name=VALUES(connector_name)""",
        (item_id, user_id))
    cur.execute("""INSERT INTO pluggy_accounts
        (id,item_id,user_id,name,type,balance,currency_code)
        VALUES (%s,%s,%s,'Neon (manual)','BANK',NULL,'BRL')
        ON DUPLICATE KEY UPDATE name=VALUES(name),balance=NULL""",
        (account_id, item_id, user_id))
    return item_id, account_id


def create_router(get_db, verify_token):
    router = APIRouter(prefix="/api/pluggy")

    @router.put("/items/{item_id}/label")
    def label_item(item_id: str, data: ItemLabelInput, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("UPDATE pluggy_items SET display_name=%s WHERE id=%s AND user_id=%s",
                        (data.display_name.strip(), item_id, user["id"]))
            if not cur.rowcount:
                raise HTTPException(404, "Banco não encontrado")
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.get("/analytics")
    def analytics(from_date: date, to_date: date, user=Depends(verify_token)):
        if to_date < from_date or (to_date - from_date).days > 366:
            raise HTTPException(400, "Selecione um período de até 12 meses")
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("""SELECT id,display_name,connector_name,source,status,last_sync
                FROM pluggy_items WHERE user_id=%s ORDER BY display_name,id""", (user["id"],))
            items = cur.fetchall()
            cur.execute("""SELECT id,item_id,name,type,balance FROM pluggy_accounts
                WHERE user_id=%s ORDER BY name""", (user["id"],))
            accounts = cur.fetchall()
            cur.execute("SELECT nome,label FROM categorias ORDER BY label")
            categories = cur.fetchall()
            cur.execute("""SELECT t.id,t.item_id,t.account_id,t.transaction_date,t.description,
                t.amount,t.direction,t.status,t.payment_method,t.recipient_name,t.user_category,
                a.name AS account_name,a.type AS account_type
                FROM pluggy_transactions t JOIN pluggy_accounts a ON a.id=t.account_id
                WHERE t.user_id=%s AND t.transaction_date BETWEEN %s AND %s
                ORDER BY t.transaction_date DESC,t.id DESC LIMIT 20001""",
                (user["id"], from_date, to_date))
            transactions = cur.fetchall()
            truncated = len(transactions) > 20000
            transactions = transactions[:20000]
            cur.execute("""SELECT b.id,b.account_id,b.item_id,b.due_date,b.total_amount,
                b.paid_amount,a.name AS account_name
                FROM pluggy_card_bills b JOIN pluggy_accounts a ON a.id=b.account_id
                WHERE b.user_id=%s AND b.due_date BETWEEN %s AND %s
                ORDER BY b.due_date DESC""", (user["id"], from_date, to_date))
            bills = cur.fetchall()
            for row in items:
                row["last_sync"] = row["last_sync"].isoformat() if row["last_sync"] else None
            for row in accounts:
                row["balance"] = float(row["balance"]) if row["balance"] is not None else None
            for row in transactions:
                row["amount"] = float(row["amount"])
                row["transaction_date"] = row["transaction_date"].isoformat()
            for row in bills:
                row["due_date"] = row["due_date"].isoformat()
                row["total_amount"] = float(row["total_amount"])
                row["paid_amount"] = float(row["paid_amount"])
            return {"items": items, "accounts": accounts, "categories": categories,
                    "transactions": transactions, "bills": bills, "truncated": truncated}
        finally:
            cur.close(); db.close()

    @router.post("/manual-import")
    async def manual_import(month: str = Form(...), file: UploadFile = File(...),
                            user=Depends(verify_token)):
        try:
            raw = await file.read(5 * 1024 * 1024 + 1)
            rows = parse_statement(raw, file.filename or "", month, f"neon:{user['id']}")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            db.start_transaction()
            item_id, account_id = ensure_manual_neon(cur, user["id"])
            inserted = 0
            for row in rows:
                cur.execute("""INSERT IGNORE INTO pluggy_transactions
                    (id,item_id,account_id,user_id,transaction_date,description,amount,
                     direction,status,payment_method)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'POSTED','EXTRATO')""",
                    (row["id"], item_id, account_id, user["id"], row["date"],
                     row["description"], abs(row["amount"]), row["direction"]))
                if cur.rowcount:
                    inserted += 1
            db.commit()
            return {"month": month, "found": len(rows), "imported": inserted,
                    "duplicates": len(rows) - inserted}
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close(); db.close()

    @router.post("/manual-entry")
    def manual_entry(data: ManualEntryInput, user=Depends(verify_token)):
        if data.direction not in ("DEBIT", "CREDIT"):
            raise HTTPException(400, "Escolha saída ou entrada")
        if len(data.description.strip()) < 2:
            raise HTTPException(400, "Informe a descrição")
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            db.start_transaction()
            item_id, account_id = ensure_manual_neon(cur, user["id"])
            transaction_id = "manual:" + uuid.uuid4().hex
            cur.execute("""INSERT INTO pluggy_transactions
                (id,item_id,account_id,user_id,transaction_date,description,amount,
                 direction,status,payment_method)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'POSTED','MANUAL')""",
                (transaction_id, item_id, account_id, user["id"],
                 data.transaction_date, data.description.strip(), data.amount,
                 data.direction))
            db.commit()
            return {"id": transaction_id}
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close(); db.close()

    @router.delete("/manual-entry/{transaction_id}")
    def delete_manual_entry(transaction_id: str, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("""DELETE t FROM pluggy_transactions t
                JOIN pluggy_items i ON i.id=t.item_id
                WHERE t.id=%s AND t.user_id=%s AND i.user_id=%s AND i.source='manual'""",
                (transaction_id, user["id"], user["id"]))
            if not cur.rowcount:
                raise HTTPException(404, "Lançamento manual não encontrado")
            return {"deleted": True}
        finally:
            cur.close(); db.close()

    @router.get("/status")
    def status(user=Depends(verify_token)):
        return {"configured": pluggy.configured(),
                "sandbox": os.getenv("PLUGGY_SANDBOX", "false").lower() == "true"}

    @router.post("/connect-token")
    def connect_token(user=Depends(verify_token)):
        try:
            return pluggy.request("POST", "/connect_token", body={
                "options": {"clientUserId": f"financas:{user['id']}", "avoidDuplicates": True}})
        except pluggy.PluggyError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/items/{item_id}/connect-token")
    def update_connect_token(item_id: str, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM pluggy_items WHERE id=%s AND user_id=%s", (item_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(404, "Conexão não encontrada")
        finally:
            cur.close(); db.close()
        try:
            return pluggy.request("POST", "/connect_token", body={"itemId": item_id,
                "options": {"clientUserId": f"financas:{user['id']}"}})
        except pluggy.PluggyError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/items/{item_id}/sync")
    def sync(item_id: str, user=Depends(verify_token)):
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", item_id):
            raise HTTPException(400, "Item inválido")
        try:
            sync_item(get_db, item_id, user["id"])
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        except pluggy.PluggyError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/import-item")
    def import_item(data: ImportInput, user=Depends(verify_token)):
        if user.get("role") != "admin":
            raise HTTPException(403, "Somente administrador pode importar itens do Dashboard")
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", data.item_id):
            raise HTTPException(400, "Item ID inválido")
        try:
            sync_item(get_db, data.item_id, user["id"], allow_unlinked=True)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        except pluggy.PluggyError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/dashboard")
    def dashboard(month: str, user=Depends(verify_token)):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "Mês inválido")
        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1)
        next_month_start = date(year + (month_number == 12), month_number % 12 + 1, 1)
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM pluggy_items WHERE user_id=%s ORDER BY created_at DESC", (user["id"],))
            items = cur.fetchall()
            cur.execute("SELECT * FROM pluggy_accounts WHERE user_id=%s ORDER BY name", (user["id"],))
            accounts = cur.fetchall()
            cur.execute("""SELECT t.*, a.name AS account_name, a.type AS account_type,
                m.gasto_id AS matched_gasto_id, m.source AS match_source
                FROM pluggy_transactions t JOIN pluggy_accounts a ON a.id=t.account_id
                LEFT JOIN pluggy_matches m ON m.transaction_id=t.id
                WHERE t.user_id=%s AND t.transaction_date >= %s AND t.transaction_date < %s
                ORDER BY t.transaction_date DESC, t.id DESC LIMIT 1000""",
                (user["id"], month_start, next_month_start))
            transactions = cur.fetchall()
            cur.execute("""SELECT r.*, g.nome AS gasto_nome FROM pluggy_rules r
                JOIN gastos g ON g.id=r.gasto_id WHERE r.user_id=%s ORDER BY g.nome""", (user["id"],))
            rules = cur.fetchall()
            cur.execute("SELECT id,nome FROM gastos WHERE user_id=%s ORDER BY nome", (user["id"],))
            gastos = cur.fetchall()
            cur.execute("SELECT nome,label FROM categorias ORDER BY label")
            categories = cur.fetchall()
            paid = {}
            cur.execute("""SELECT m.gasto_id, COALESCE(SUM(ABS(t.amount)),0) AS paid
                FROM pluggy_matches m JOIN pluggy_transactions t ON t.id=m.transaction_id
                WHERE m.user_id=%s AND m.mes_ref=%s AND t.status='POSTED'
                GROUP BY m.gasto_id""", (user["id"], month))
            for row in cur.fetchall():
                paid[row["gasto_id"]] = Decimal(str(row["paid"]))
            cur.execute("""SELECT bg.gasto_id,
                COALESCE(SUM(LEAST(b.total_amount,b.paid_amount)),0) AS paid
                FROM pluggy_card_bills b JOIN pluggy_bill_gastos bg ON bg.account_id=b.account_id
                WHERE b.user_id=%s AND b.due_date >= %s AND b.due_date < %s
                GROUP BY bg.gasto_id""", (user["id"], month_start, next_month_start))
            for row in cur.fetchall():
                paid[row["gasto_id"]] = max(paid.get(row["gasto_id"], Decimal("0")),
                                            Decimal(str(row["paid"])))
            forecasts = []
            for gasto in gastos:
                expected = expected_amount(cur, user["id"], gasto["id"], month)
                if expected > 0:
                    amount_paid = paid.get(gasto["id"], Decimal("0"))
                    forecasts.append({"gasto_id": gasto["id"], "name": gasto["nome"],
                        "expected": float(expected), "paid": float(amount_paid),
                        "remaining": float(max(Decimal("0"), expected - amount_paid))})
            for row in accounts:
                row["balance"] = float(row["balance"]) if row["balance"] is not None else None
            for row in transactions:
                row["amount"] = float(row["amount"])
                row["transaction_date"] = row["transaction_date"].isoformat()
                row.pop("updated_at", None)
            for row in rules:
                row["tolerance"] = float(row["tolerance"])
            for row in items:
                row["last_sync"] = row["last_sync"].isoformat() if row["last_sync"] else None
                row["created_at"] = row["created_at"].isoformat()
            return {"items": items, "accounts": accounts, "transactions": transactions,
                    "rules": rules, "gastos": gastos, "categories": categories,
                    "forecasts": forecasts}
        finally:
            cur.close(); db.close()

    @router.put("/rules")
    def save_rule(data: RuleInput, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM gastos WHERE id=%s AND user_id=%s", (data.gasto_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(404, "Gasto fixo não encontrado")
            cur.execute("""INSERT INTO pluggy_rules
                (user_id,gasto_id,recipient_name,recipient_document,tolerance)
                VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                recipient_name=VALUES(recipient_name),recipient_document=VALUES(recipient_document),
                tolerance=VALUES(tolerance)""",
                (user["id"], data.gasto_id, data.recipient_name.strip(),
                 only_digits(data.recipient_document), data.tolerance))
            cur.execute("""SELECT t.id FROM pluggy_transactions t
                JOIN pluggy_accounts a ON a.id=t.account_id
                WHERE t.user_id=%s AND t.status='POSTED' AND t.direction='DEBIT'
                AND a.type='BANK' AND t.auto_ignored=0 AND
                (t.recipient_name IS NOT NULL OR t.recipient_document IS NOT NULL
                 OR (t.payment_method='PIX' AND
                     (t.description LIKE 'Pix enviado %' OR
                      t.description LIKE 'Transferência enviada%')))""",
                (user["id"],))
            for txn in cur.fetchall():
                auto_match(cur, user["id"], txn["id"])
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.delete("/rules/{gasto_id}")
    def delete_rule(gasto_id: int, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("DELETE FROM pluggy_rules WHERE gasto_id=%s AND user_id=%s", (gasto_id, user["id"]))
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.post("/matches")
    def match(data: MatchInput, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM gastos WHERE id=%s AND user_id=%s", (data.gasto_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(404, "Gasto fixo não encontrado")
            cur.execute("""SELECT t.transaction_date,t.status,t.direction,a.type
                FROM pluggy_transactions t JOIN pluggy_accounts a ON a.id=t.account_id
                WHERE t.id=%s AND t.user_id=%s""", (data.transaction_id, user["id"]))
            txn = cur.fetchone()
            if not txn or txn["status"] != "POSTED" or txn["direction"] != "DEBIT" or txn["type"] != "BANK":
                raise HTTPException(400, "Escolha uma saída efetivada de conta bancária")
            month = str(txn["transaction_date"])[:7]
            cur.execute("""INSERT INTO pluggy_matches
                (user_id,gasto_id,mes_ref,transaction_id,source) VALUES (%s,%s,%s,%s,'manual')
                ON DUPLICATE KEY UPDATE gasto_id=VALUES(gasto_id),mes_ref=VALUES(mes_ref),source='manual'""",
                (user["id"], data.gasto_id, month, data.transaction_id))
            cur.execute("UPDATE pluggy_transactions SET auto_ignored=0 WHERE id=%s AND user_id=%s",
                        (data.transaction_id, user["id"]))
            cur.execute("""UPDATE pluggy_transactions t JOIN gastos g ON g.id=%s
                SET t.user_category=COALESCE(t.user_category,g.cat)
                WHERE t.id=%s AND t.user_id=%s""",
                (data.gasto_id, data.transaction_id, user["id"]))
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.delete("/matches/{transaction_id}")
    def unmatch(transaction_id: str, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("DELETE FROM pluggy_matches WHERE transaction_id=%s AND user_id=%s",
                        (transaction_id, user["id"]))
            cur.execute("UPDATE pluggy_transactions SET auto_ignored=1 WHERE id=%s AND user_id=%s",
                        (transaction_id, user["id"]))
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.put("/transactions/{transaction_id}/category")
    def set_category(transaction_id: str, data: CategoryInput, user=Depends(verify_token)):
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            if data.category:
                cur.execute("SELECT nome FROM categorias WHERE nome=%s", (data.category,))
                if not cur.fetchone():
                    raise HTTPException(400, "Categoria inválida")
            cur.execute("SELECT id FROM pluggy_transactions WHERE id=%s AND user_id=%s",
                        (transaction_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(404, "Transação não encontrada")
            cur.execute("UPDATE pluggy_transactions SET user_category=%s WHERE id=%s AND user_id=%s",
                        (data.category or None, transaction_id, user["id"]))
            return {"ok": True}
        finally:
            cur.close(); db.close()

    @router.post("/webhook")
    async def webhook(request: Request, x_webhook_secret: str = Header(default="")):
        secret = os.getenv("PLUGGY_WEBHOOK_SECRET", "")
        if not secret or not hmac.compare_digest(x_webhook_secret, secret):
            raise HTTPException(401, "Webhook não autorizado")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Evento inválido")
        event_id = body.get("eventId")
        if not isinstance(event_id, str) or len(event_id) > 80:
            raise HTTPException(400, "Evento inválido")
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("""INSERT IGNORE INTO pluggy_webhook_events
                (event_id,item_id,event_name,payload) VALUES (%s,%s,%s,%s)""",
                (event_id, body.get("itemId"), str(body.get("event", ""))[:80], json.dumps(body)))
            return {"ok": True}
        finally:
            cur.close(); db.close()

    return router
