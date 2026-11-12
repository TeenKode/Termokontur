#!/usr/bin/env bash
# Install the web dashboard natively on Debian 12/13 (including Proxmox LXC).
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run as root: sudo bash scripts/install-debian.sh"; exit 1; fi
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/termokontur"
CONFIG_DIR="/etc/termokontur"
CONFIG_FILE="${CONFIG_DIR}/termokontur.env"
echo "[1/6] Installing Python system packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip ca-certificates git curl
echo "[2/6] Creating restricted service account..."
if ! id termokontur >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin termokontur
fi
install -d -o termokontur -g termokontur -m 0750 "${APP_DIR}" /var/lib/termokontur
install -d -o root -g root -m 0700 "${CONFIG_DIR}"
echo "[3/6] Copying application..."
cp -a "${ROOT_DIR}/termokontur" "${APP_DIR}/"
cp "${ROOT_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
chown -R termokontur:termokontur "${APP_DIR}"
echo "[4/6] Installing isolated Python dependencies..."
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --no-cache-dir -r "${APP_DIR}/requirements.txt"
chown -R root:root "${APP_DIR}"
chmod -R a+rX "${APP_DIR}"
echo "[5/6] Preparing configuration..."
if [ ! -e "${CONFIG_FILE}" ]; then
  python3 - "${CONFIG_FILE}" <<'PY'
import secrets, sys
with open(sys.argv[1], "x", encoding="utf-8") as fp:
    fp.write("TERMOKONTUR_ADMIN_PASSWORD=" + secrets.token_urlsafe(20) + "\n")
    fp.write("TERMOKONTUR_SECRET=" + secrets.token_urlsafe(48) + "\n")
    fp.write("TERMOKONTUR_INGEST_KEY=" + secrets.token_urlsafe(32) + "\n")
    fp.write("TERMOKONTUR_STALE_HOURS=26\n")
    fp.write("TERMOKONTUR_TZ=Asia/Vladivostok\n")
    fp.write("TERMOKONTUR_REMINDERS_ENABLED=0\n")
    fp.write("TERMOKONTUR_REMINDER_TIME=09:00\n")
    fp.write("TERMOKONTUR_REMINDER_DAYS=0,1,2,3,4\n")
    fp.write("TERMOKONTUR_REMINDER_WINDOW_MINUTES=120\n")
    fp.write("TERMOKONTUR_SECURE_COOKIE=0\n")
    fp.write("MAX_BOT_TOKEN=\n")
PY
  chmod 0600 "${CONFIG_FILE}"
  echo
  echo "Initial administrator password (save securely):"
  sed -n 's/^TERMOKONTUR_ADMIN_PASSWORD=//p' "${CONFIG_FILE}"
  echo
else
  echo "Existing credentials preserved: ${CONFIG_FILE}"
fi
echo "[6/6] Enabling systemd services..."
install -m 0644 "${ROOT_DIR}/deploy/systemd/termokontur.service" /etc/systemd/system/termokontur.service
install -m 0644 "${ROOT_DIR}/deploy/systemd/termokontur-max.service" /etc/systemd/system/termokontur-max.service
install -m 0644 "${ROOT_DIR}/deploy/systemd/termokontur-update.path" /etc/systemd/system/termokontur-update.path
install -m 0644 "${ROOT_DIR}/deploy/systemd/termokontur-update.service" /etc/systemd/system/termokontur-update.service
install -o root -g root -m 0755 "${ROOT_DIR}/scripts/termokontur-apply-update" /usr/local/sbin/termokontur-apply-update
if git -C "${ROOT_DIR}" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -C "${ROOT_DIR}" rev-parse HEAD | runuser -u termokontur -- tee /var/lib/termokontur/installed-revision >/dev/null
  chmod 0600 /var/lib/termokontur/installed-revision
fi
systemctl daemon-reload
systemctl enable termokontur.service termokontur-max.service termokontur-update.path
systemctl restart termokontur.service
systemctl restart termokontur-max.service
systemctl enable --now termokontur-update.path
echo
echo "Installed. Open http://<LXC-IP>:8092/ in your LAN."
echo "Check: systemctl status termokontur --no-pager"
echo "Manage MAX, passwords, reminders and updates in the web panel's Settings tab."
echo "Configuration secrets are imported from ${CONFIG_FILE} once, then managed in SQLite."
