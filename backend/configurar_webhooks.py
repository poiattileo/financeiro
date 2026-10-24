"""Register idempotent Pluggy webhook subscriptions after HTTPS is active."""
import os
from urllib.parse import urlparse

import pluggy_client as pluggy

EVENTS = (
    "item/created", "item/updated", "item/deleted", "item/error",
    "transactions/created", "transactions/updated", "transactions/deleted",
)


def main():
    env_file = "/opt/financeiro/backend/.env"
    with open(env_file, encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.strip().split("=", 1)
                os.environ.setdefault(key, value)
    url = os.getenv("PLUGGY_WEBHOOK_URL", "")
    secret = os.getenv("PLUGGY_WEBHOOK_SECRET", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not secret:
        raise SystemExit("Configure PLUGGY_WEBHOOK_URL (HTTPS) e PLUGGY_WEBHOOK_SECRET no .env")
    response = pluggy.request("GET", "/webhooks")
    existing = response.get("results", []) if isinstance(response, dict) else response
    for event in EVENTS:
        if any(w.get("url") == url and w.get("event") == event for w in existing):
            print("Já cadastrado:", event)
            continue
        pluggy.request("POST", "/webhooks", body={"url": url, "event": event,
            "headers": {"X-Webhook-Secret": secret}})
        print("Cadastrado:", event)


if __name__ == "__main__":
    main()
