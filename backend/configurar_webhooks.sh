#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=/opt/financeiro/backend/.env
if [ ! -f "$ENV_FILE" ]; then echo "Instale o projeto primeiro."; exit 1; fi
cd /opt/financeiro/backend
/opt/financeiro/venv/bin/python configurar_webhooks.py
