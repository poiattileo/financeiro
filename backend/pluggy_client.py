"""Small server-side Pluggy client. No application secret is ever sent to the browser."""
import os
import threading
import time
from urllib.parse import urljoin

import requests


BASE = "https://api.pluggy.ai"
_lock = threading.Lock()
_key = None
_expires = 0.0


class PluggyError(Exception):
    pass


def configured():
    return bool(os.getenv("PLUGGY_CLIENT_ID") and os.getenv("PLUGGY_CLIENT_SECRET"))


def api_key():
    global _key, _expires
    if not configured():
        raise PluggyError("Configure PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET no .env")
    with _lock:
        if _key and time.time() < _expires:
            return _key
        try:
            response = requests.post(
                BASE + "/auth",
                json={"clientId": os.environ["PLUGGY_CLIENT_ID"],
                      "clientSecret": os.environ["PLUGGY_CLIENT_SECRET"]},
                timeout=20,
            )
            response.raise_for_status()
            _key = response.json()["apiKey"]
            _expires = time.time() + 105 * 60
            return _key
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise PluggyError("Falha na autenticação com a Pluggy") from exc


def request(method, path, *, params=None, body=None):
    if not path.startswith("/") or path.startswith("//"):
        raise PluggyError("Caminho inválido da API Pluggy")
    try:
        response = requests.request(
            method, urljoin(BASE, path), params=params, json=body,
            headers={"X-API-KEY": api_key()}, timeout=35,
        )
        response.raise_for_status()
        return response.json() if response.content else {}
    except (requests.RequestException, ValueError) as exc:
        raise PluggyError("A Pluggy não respondeu à solicitação") from exc


def list_transactions(account_id):
    """Follow opaque cursor strings returned by /v2/transactions."""
    path = "/v2/transactions"
    params = {"accountId": account_id}
    for _ in range(100):
        data = request("GET", path, params=params)
        yield from data.get("results", [])
        next_page = data.get("next")
        if not next_page:
            return
        if not next_page.startswith("?"):
            raise PluggyError("Cursor inválido da Pluggy")
        path = "/v2/transactions" + next_page
        params = None
    raise PluggyError("A importação excedeu 100 páginas; tente novamente")
