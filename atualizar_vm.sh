#!/usr/bin/env bash
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "Use: sudo bash atualizar_vm.sh"; exit 1; fi
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/financeiro
if [ ! -f "$APP_DIR/backend/.env" ]; then
  echo "Instale primeiro com instalar_vm.sh"; exit 1
fi
mkdir -p "$APP_DIR/backups"
BACKUP="$APP_DIR/backups/financeiro-$(date +%Y%m%d-%H%M%S).sql"
mysqldump --single-transaction financeiro > "$BACKUP"
echo "Backup: $BACKUP"
cp "$SRC_DIR"/backend/*.py "$SRC_DIR"/backend/*.sql "$SRC_DIR"/backend/*.sh "$APP_DIR/backend/"
cp "$SRC_DIR"/backend/requirements.txt "$APP_DIR/backend/"
cp "$SRC_DIR"/frontend/* "$APP_DIR/frontend/"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"
mysql --default-character-set=utf8mb4 financeiro < "$APP_DIR/backend/schema_base.sql"
mysql --default-character-set=utf8mb4 financeiro < "$APP_DIR/backend/pluggy_schema.sql"
mysql --default-character-set=utf8mb4 financeiro < "$APP_DIR/backend/pluggy_migrations.sql"
chown -R root:root "$APP_DIR/backend" "$APP_DIR/frontend"
chown root:www-data "$APP_DIR/backend/.env"
chmod 640 "$APP_DIR/backend/.env"
systemctl restart financeiro financeiro-pluggy-worker
if systemctl is-enabled --quiet financeiro-bot; then systemctl restart financeiro-bot; fi
systemctl is-active --quiet financeiro
echo "Atualização concluída. Banco preservado e backup criado."
