#!/usr/bin/env bash
# Run on the VM from a private terminal. Reads the Pluggy secret without echoing it.
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Execute como root: sudo bash configurar_pluggy_vm.sh" >&2
  exit 1
fi

ENV_FILE=/opt/financeiro/backend/.env
PYTHON=/opt/financeiro/venv/bin/python
if [ ! -f "$ENV_FILE" ] || [ ! -x "$PYTHON" ]; then
  echo "Instale o Financeiro antes de configurar a Pluggy." >&2
  exit 1
fi

client_id="$(sed -n 's/^PLUGGY_CLIENT_ID=//p' "$ENV_FILE" | head -n 1)"
if [ -z "$client_id" ]; then
  read -r -p "Client ID da aplicação Pluggy: " client_id
fi
if [[ ! "$client_id" =~ ^[[:xdigit:]-]{36}$ ]]; then
  echo "Client ID inválido." >&2
  exit 1
fi

read -r -s -p "Cole o Client Secret da Pluggy: " client_secret
printf '\n'
if [ -z "$client_secret" ] || [[ "$client_secret" == *$'\r'* ]]; then
  echo "Client Secret vazio ou inválido." >&2
  exit 1
fi

export PLUGGY_SETUP_CLIENT_ID="$client_id"
export PLUGGY_SETUP_CLIENT_SECRET="$client_secret"
"$PYTHON" - <<'PY'
import os
import sys
import requests

try:
    response = requests.post(
        "https://api.pluggy.ai/auth",
        json={
            "clientId": os.environ["PLUGGY_SETUP_CLIENT_ID"],
            "clientSecret": os.environ["PLUGGY_SETUP_CLIENT_SECRET"],
        },
        timeout=20,
    )
    response.raise_for_status()
    if not response.json().get("apiKey"):
        raise ValueError("Resposta sem chave de API")
except (requests.RequestException, ValueError):
    print("Não foi possível validar as credenciais com a Pluggy. Nada foi alterado.", file=sys.stderr)
    sys.exit(1)
print("Credenciais validadas com a Pluggy.")
PY

"$PYTHON" - "$ENV_FILE" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
original = path.read_text()
values = {
    "PLUGGY_CLIENT_ID": os.environ["PLUGGY_SETUP_CLIENT_ID"],
    "PLUGGY_CLIENT_SECRET": os.environ["PLUGGY_SETUP_CLIENT_SECRET"],
}
for value in values.values():
    if any(char in value for char in "\r\n\0"):
        sys.exit("Credencial contém caractere inválido; nada foi alterado.")

lines = []
seen = set()
for line in original.splitlines():
    key = line.split("=", 1)[0]
    if key in values:
        if key not in seen:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
    else:
        lines.append(line)
for key, value in values.items():
    if key not in seen:
        lines.append(f"{key}={value}")

metadata = path.stat()
with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".env.", delete=False) as temp:
    temp.write("\n".join(lines) + "\n")
    os.fchmod(temp.fileno(), metadata.st_mode & 0o777)
    os.fchown(temp.fileno(), metadata.st_uid, metadata.st_gid)
    temp_path = temp.name
os.replace(temp_path, path)
PY

unset client_secret PLUGGY_SETUP_CLIENT_SECRET
systemctl restart financeiro financeiro-pluggy-worker
systemctl is-active --quiet financeiro
systemctl is-active --quiet financeiro-pluggy-worker
echo "Pluggy configurada. Volte ao app e importe cada Item ID na aba Bancos."
