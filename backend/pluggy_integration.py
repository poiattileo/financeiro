"""Pluggy sync, matching and FastAPI endpoints for the existing finance app."""
import json
import logging
import os
import re
import unicodedata
import hmac
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import pluggy_client as pluggy

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


def pix_recipient(description, payment_method):
    if str(payment_method or "").upper() != "PIX":
        return None
    match = re.match(r"^\s*pix\s+enviado(?:\s+para)?\s+(.+?)\s*$", description or "", re.I)
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
    recipient_name = txn["recipient_name"] or pix_recipient(txn["description"], txn["payment_method"])
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
                        txn.get("description"), payment_method)
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


def create_router(get_db, verify_token):
    router = APIRouter(prefix="/api/pluggy")

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
                 OR (t.payment_method='PIX' AND t.description LIKE 'Pix enviado %'))""",
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
