#!/usr/bin/env bash
# MOR Userbot Manager installer for Ubuntu/Debian/Kali servers and WSL.
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh <BOT_TOKEN> <OWNER_NUMERIC_ID>"
  exit 1
fi

BOT_TOKEN="${1:-}"
OWNER_ID="${2:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/morself-manager"
STATE_DIR="/var/lib/morself-manager"
SERVICE_USER="morself"
ENV_FILE="/etc/morself-manager.env"
SERVICE_FILE="/etc/systemd/system/morself-manager.service"

if [[ -z "${BOT_TOKEN}" || -z "${OWNER_ID}" ]]; then
  echo "Usage: sudo ./install.sh <BOT_TOKEN> <OWNER_NUMERIC_ID>"
  exit 1
fi
if [[ ! "${BOT_TOKEN}" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
  echo "Invalid BotFather token format."
  exit 1
fi
if [[ ! "${OWNER_ID}" =~ ^[1-9][0-9]{0,19}$ ]]; then
  echo "OWNER_NUMERIC_ID must be a positive numeric Telegram user ID."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=60
export PIP_NO_INPUT=1

# Do not make a healthy Python installation depend on a broken third-party apt mirror.
# This is important on Kali/WSL, where apt-get update can block or fail before the
# actual application installation starts.
PYTHON_OK=0
if command -v python3 >/dev/null 2>&1; then
  TMP_VENV="$(mktemp -d /tmp/morself-venv-check.XXXXXX)"
  if python3 -m venv "${TMP_VENV}" >/dev/null 2>&1; then
    PYTHON_OK=1
  fi
  rm -rf "${TMP_VENV}"
fi

if [[ ${PYTHON_OK} -eq 0 ]]; then
  echo "Python venv support is missing; trying apt with a 60-second timeout."
  if ! timeout --foreground 60 apt-get update; then
    echo "WARNING: apt-get update failed. Your apt sources/mirror are broken or unreachable."
    echo "Fix the Kali repository, then rerun this installer."
    echo "Expected Kali source format: deb https://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware"
    exit 1
  fi
  timeout --foreground 120 apt-get install -y python3 python3-venv python3-pip ca-certificates
else
  echo "Python 3 + venv detected; skipping apt-get update."
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; installation will use a background launcher instead."
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "${STATE_DIR}" --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0755 "${INSTALL_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${STATE_DIR}/data"
install -m 0644 "${SCRIPT_DIR}/MORSELF.py" "${INSTALL_DIR}/MORSELF.py"
install -m 0644 "${SCRIPT_DIR}/manager_bot.py" "${INSTALL_DIR}/manager_bot.py"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
install -m 0644 "${SCRIPT_DIR}/test_manager.py" "${INSTALL_DIR}/test_manager.py"
install -m 0644 "${SCRIPT_DIR}/README.md" "${INSTALL_DIR}/README.md"

if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
# Do not upgrade pip here: it is unnecessary and makes installation depend on
# downloading a large pip wheel before the actual application can start.
# Retries handle transient proxy disconnects such as IncompleteRead/407.
"${INSTALL_DIR}/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-cache-dir --retries 6 --timeout 60 \
  -r "${INSTALL_DIR}/requirements.txt"
chown -R root:root "${INSTALL_DIR}"
chmod -R go-w "${INSTALL_DIR}"

cat > "${ENV_FILE}" <<EOF
# Read only by root/systemd. Do not share this file.
MOR_BOT_TOKEN=${BOT_TOKEN}
MOR_OWNER_ID=${OWNER_ID}
MOR_DATA_DIR=${STATE_DIR}/data
EOF
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"

cat > "${SERVICE_FILE}" <<'EOF'
[Unit]
Description=MOR Userbot Manager (owner-only Telegram panel)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=morself
Group=morself
WorkingDirectory=/opt/morself-manager
EnvironmentFile=/etc/morself-manager.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/morself-manager/.venv/bin/python /opt/morself-manager/manager_bot.py --data-dir /var/lib/morself-manager/data
Restart=always
RestartSec=5
StartLimitIntervalSec=0
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/morself-manager
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "${SERVICE_FILE}"

if command -v systemctl >/dev/null 2>&1 && [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]; then
  systemctl daemon-reload
  systemctl enable --now morself-manager.service
  sleep 2
  systemctl --no-pager --full status morself-manager.service || true
  echo "Service mode: systemd"
else
  # WSL and minimal containers often have no running systemd PID 1.
  # Keep the same app running in the background and persist its PID.
  RUN_SCRIPT="${STATE_DIR}/start-background.sh"
  cat > "${RUN_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE_DIR="/var/lib/morself-manager/data"
PID_FILE="${STATE_DIR}/manager.pid"
LOG_FILE="${STATE_DIR}/manager.log"
if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "MOR manager is already running (PID $(cat "${PID_FILE}"))"
  exit 0
fi
nohup /opt/morself-manager/.venv/bin/python /opt/morself-manager/manager_bot.py --data-dir "${STATE_DIR}" >>"${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"
echo "MOR manager started (PID $!)"
EOF
  cat > "${STATE_DIR}/stop-background.sh" <<'EOF'
#!/usr/bin/env bash
set -u
PID_FILE="/var/lib/morself-manager/data/manager.pid"
if [[ -f "${PID_FILE}" ]]; then
  kill "$(cat "${PID_FILE}")" 2>/dev/null || true
  rm -f "${PID_FILE}"
  echo "MOR manager stopped"
else
  echo "MOR manager is not running"
fi
EOF
  chmod 0750 "${RUN_SCRIPT}" "${STATE_DIR}/stop-background.sh"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${RUN_SCRIPT}" "${STATE_DIR}/stop-background.sh"
  su -s /bin/bash -c "${RUN_SCRIPT}" "${SERVICE_USER}"
  echo "Service mode: background launcher (systemd is not running)"
  echo "Start: ${RUN_SCRIPT}"
  echo "Stop : ${STATE_DIR}/stop-background.sh"
  echo "Log  : ${STATE_DIR}/data/manager.log"
fi

echo
echo "Installed successfully. Open the bot, then send /start from Telegram account ID ${OWNER_ID}."
echo "After first login: Login account -> phone -> code -> 2FA password (if enabled)."
echo "Systemd logs: sudo journalctl -u morself-manager -f"
