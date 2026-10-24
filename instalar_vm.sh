#!/usr/bin/env bash
# Ubuntu 22.04/24.04. Installs separately from the original /opt/financas app.
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then echo "Use: sudo bash instalar_vm.sh"; exit 1; fi
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/financeiro
DB_NAME=financeiro
DB_USER=financeiro
DOMAIN="${APP_DOMAIN:-}"
EMAIL="${LETSENCRYPT_EMAIL:-}"

apt-get update
apt-get install -y nginx mariadb-server python3 python3-pip python3-venv openssl curl
systemctl enable --now mariadb nginx
mkdir -p "$APP_DIR/backend" "$APP_DIR/frontend" "$APP_DIR/uploads"

ENV_FILE="$APP_DIR/backend/.env"
if [ -f "$ENV_FILE" ]; then
  DB_PASS="$(sed -n 's/^DB_PASS=//p' "$ENV_FILE" | head -n 1)"
  JWT_SECRET="$(sed -n 's/^JWT_SECRET=//p' "$ENV_FILE" | head -n 1)"
  WEBHOOK_SECRET="$(sed -n 's/^PLUGGY_WEBHOOK_SECRET=//p' "$ENV_FILE" | head -n 1)"
else
  DB_PASS="$(openssl rand -hex 20)"
  JWT_SECRET="$(openssl rand -hex 32)"
  WEBHOOK_SECRET="$(openssl rand -hex 32)"
fi

cp "$SRC_DIR"/backend/*.py "$SRC_DIR"/backend/*.sql "$APP_DIR/backend/"
cp "$SRC_DIR"/backend/*.sh "$APP_DIR/backend/"
cp "$SRC_DIR"/frontend/* "$APP_DIR/frontend/"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
DB_HOST=localhost
DB_USER=$DB_USER
DB_PASS=$DB_PASS
DB_NAME=$DB_NAME
JWT_SECRET=$JWT_SECRET
UPLOAD_DIR=$APP_DIR/uploads
PLUGGY_CLIENT_ID=
PLUGGY_CLIENT_SECRET=
PLUGGY_WEBHOOK_SECRET=$WEBHOOK_SECRET
PLUGGY_WEBHOOK_URL=
PLUGGY_SANDBOX=true
TELEGRAM_BOT_TOKEN=
EOF
fi
chown root:www-data "$ENV_FILE"
chmod 640 "$ENV_FILE"

mysql <<SQL
CREATE DATABASE IF NOT EXISTS $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
mysql "$DB_NAME" < "$APP_DIR/backend/schema_base.sql"
mysql "$DB_NAME" < "$APP_DIR/backend/pluggy_schema.sql"
mysql "$DB_NAME" < "$APP_DIR/backend/pluggy_migrations.sql"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then python3 -m venv "$APP_DIR/venv"; fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"
chown -R root:root "$APP_DIR/backend" "$APP_DIR/frontend"
chown root:www-data "$ENV_FILE"
chmod 640 "$ENV_FILE"
chown -R www-data:www-data "$APP_DIR/uploads"

cat > /etc/systemd/system/financeiro.service <<EOF
[Unit]
Description=Financeiro com Pluggy API
After=network.target mariadb.service
Requires=mariadb.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/financeiro-pluggy-worker.service <<EOF
[Unit]
Description=Financeiro Pluggy webhook worker
After=network.target mariadb.service
Requires=mariadb.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/backend/pluggy_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/financeiro-bot.service <<EOF
[Unit]
Description=Financeiro Telegram bot (optional)
After=network.target financeiro.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/backend/bot_telegram.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now financeiro financeiro-pluggy-worker
systemctl restart financeiro financeiro-pluggy-worker

if [ -n "$DOMAIN" ]; then
  SERVER_NAME="$DOMAIN"
  LISTEN=80
else
  SERVER_NAME="_"
  LISTEN=127.0.0.1:8081
fi
cat > /etc/nginx/sites-available/financeiro <<EOF
server {
    listen $LISTEN;
    server_name $SERVER_NAME;
    root $APP_DIR/frontend;
    index index.html;
    client_max_body_size 20M;
    location / { try_files \$uri \$uri/ /index.html; }
    location /api/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 180s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/financeiro /etc/nginx/sites-enabled/financeiro
nginx -t
systemctl reload nginx

if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ]; then
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx --non-interactive --agree-tos --redirect -m "$EMAIL" -d "$DOMAIN"
  if grep -q '^PLUGGY_WEBHOOK_URL=$' "$ENV_FILE"; then
    sed -i "s#^PLUGGY_WEBHOOK_URL=$#PLUGGY_WEBHOOK_URL=https://$DOMAIN/api/pluggy/webhook#" "$ENV_FILE"
  fi
  systemctl restart financeiro financeiro-pluggy-worker
fi

COUNT="$(mysql -N "$DB_NAME" -e 'SELECT COUNT(*) FROM usuarios')"
if [ "$COUNT" = "0" ]; then
  echo "Crie o primeiro usuário (administrador):"
  DB_HOST=localhost DB_USER="$DB_USER" DB_PASS="$DB_PASS" DB_NAME="$DB_NAME" \
    "$APP_DIR/venv/bin/python" "$APP_DIR/backend/criar_usuario.py"
fi

if [ -n "$DOMAIN" ]; then
  echo "Aplicação: https://$DOMAIN"
else
  echo "Teste por túnel SSH: ssh -L 8081:127.0.0.1:8081 USUARIO@IP_DA_VM"
  echo "Depois abra http://127.0.0.1:8081 no seu navegador."
  echo "Configure um domínio e HTTPS antes de conectar contas reais ou ativar webhooks."
fi
echo "Edite $ENV_FILE para inserir CLIENT_ID e CLIENT_SECRET da Pluggy."
echo "Depois rode: sudo systemctl restart financeiro financeiro-pluggy-worker"
echo "Com HTTPS ativo, rode: sudo bash $APP_DIR/backend/configurar_webhooks.sh"
