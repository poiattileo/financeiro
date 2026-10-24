"""Durable webhook queue consumer. Run as a separate systemd service."""
import json
import logging
import time

import mysql.connector

import pluggy_client as pluggy
from pluggy_integration import sync_item

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pluggy_worker")


def get_db():
    import os
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"), user=os.getenv("DB_USER", "financeiro"),
        password=os.getenv("DB_PASS", ""), database=os.getenv("DB_NAME", "financeiro"),
        autocommit=True,
    )


def process_event(event):
    payload = json.loads(event["payload"])
    item_id = event["item_id"]
    if not item_id:
        return
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT user_id FROM pluggy_items WHERE id=%s", (item_id,))
        owner = cur.fetchone()
    finally:
        cur.close(); db.close()
    if not owner:
        remote = pluggy.request("GET", "/items/" + item_id)
        external_id = remote.get("clientUserId") or ""
        if not external_id.startswith("financas:") or not external_id[9:].isdigit():
            return
        user_id = int(external_id[9:])
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM usuarios WHERE id=%s", (user_id,))
            if not cur.fetchone():
                return
        finally:
            cur.close(); db.close()
    else:
        user_id = owner["user_id"]
    if event["event_name"] == "item/deleted":
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("UPDATE pluggy_items SET status='DELETED' WHERE id=%s AND user_id=%s",
                        (item_id, user_id))
        finally:
            cur.close(); db.close()
        return
    if event["event_name"] == "transactions/deleted":
        ids = payload.get("transactionIds") or []
        db = get_db(); cur = db.cursor()
        try:
            for txn_id in ids:
                cur.execute("DELETE FROM pluggy_transactions WHERE id=%s AND item_id=%s AND user_id=%s",
                            (txn_id, item_id, user_id))
        finally:
            cur.close(); db.close()
        return
    if event["event_name"] in ("item/created", "item/updated", "transactions/created", "transactions/updated"):
        sync_item(get_db, item_id, user_id)
    elif event["event_name"] == "item/error":
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("UPDATE pluggy_items SET status='ERROR' WHERE id=%s AND user_id=%s",
                        (item_id, user_id))
        finally:
            cur.close(); db.close()


def run_once():
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""UPDATE pluggy_webhook_events SET state='queued'
            WHERE state='processing' AND next_attempt < UTC_TIMESTAMP() - INTERVAL 15 MINUTE""")
        cur.execute("""SELECT * FROM pluggy_webhook_events
            WHERE state='queued' AND next_attempt <= UTC_TIMESTAMP()
            ORDER BY created_at LIMIT 1""")
        event = cur.fetchone()
        if not event:
            return False
        cur.execute("""UPDATE pluggy_webhook_events SET state='processing',
            next_attempt=UTC_TIMESTAMP(), attempts=attempts+1
            WHERE event_id=%s AND state='queued'""", (event["event_id"],))
        if not cur.rowcount:
            return True
    finally:
        cur.close(); db.close()
    try:
        process_event(event)
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("UPDATE pluggy_webhook_events SET state='done',last_error=NULL WHERE event_id=%s",
                        (event["event_id"],))
        finally:
            cur.close(); db.close()
    except Exception as exc:
        log.exception("Failed Pluggy webhook %s", event["event_id"])
        db = get_db(); cur = db.cursor()
        try:
            delay = min(3600, 60 * (2 ** min(event["attempts"], 6)))
            cur.execute("""UPDATE pluggy_webhook_events SET state=%s,
                next_attempt=UTC_TIMESTAMP()+INTERVAL %s SECOND,last_error=%s
                WHERE event_id=%s""",
                ("failed" if event["attempts"] >= 7 else "queued", delay,
                 str(exc)[:300], event["event_id"]))
        finally:
            cur.close(); db.close()
    return True


def refresh_personal_items():
    """Read Meu Pluggy proxy items daily as a fallback to webhook delivery."""
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""SELECT id,user_id FROM pluggy_items WHERE source='dashboard'
            AND status<>'DELETED' AND (last_sync IS NULL OR last_sync<UTC_TIMESTAMP()-INTERVAL 24 HOUR)
            LIMIT 20""")
        items = cur.fetchall()
    finally:
        cur.close(); db.close()
    for item in items:
        try:
            sync_item(get_db, item["id"], item["user_id"])
        except Exception:
            log.exception("Periodic sync failed for item %s", item["id"])


if __name__ == "__main__":
    next_personal_check = 0.0
    while True:
        try:
            processed = run_once()
            if time.monotonic() >= next_personal_check:
                refresh_personal_items()
                next_personal_check = time.monotonic() + 6 * 3600
            if not processed:
                time.sleep(5)
        except Exception:
            log.exception("Worker loop failed")
            time.sleep(10)
