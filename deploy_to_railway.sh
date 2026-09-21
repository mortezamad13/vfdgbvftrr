#!/usr/bin/env bash
# MORSELF Railway deploy helper
# Uses the official Railway CLI. It creates a project/service, connects GitHub,
# sets secrets, creates a persistent /data volume, and redeploys.
set -Eeuo pipefail
trap 'unset RAILWAY_API_TOKEN railway_token bot_token owner_id' EXIT

fail() {
  echo "خطا: $*" >&2
  exit 1
}

command_exists() { command -v "$1" >/dev/null 2>&1; }

if [[ "${EUID}" -eq 0 ]]; then
  echo "هشدار: این اسکریپت را معمولاً با کاربر عادی اجرا کن، نه با sudo."
fi

if ! command_exists railway; then
  cat >&2 <<'EOF'
Railway CLI پیدا نشد.
طبق مستندات رسمی Railway یکی از این روش‌ها را اجرا کن و سپس اسکریپت را دوباره اجرا کن:

  bash <(curl -fsSL railway.com/install.sh)
  npm i -g @railway/cli
EOF
  exit 1
fi

normalize_repo() {
  local value="$1"
  value="${value#https://}"
  value="${value#http://}"
  value="${value#git@github.com:}"
  value="${value#github.com/}"
  value="${value%%\?*}"
  value="${value%%#*}"
  value="${value%.git}"
  [[ "$value" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s\n' "$value"
}

prompt_secret() {
  local label="$1" value
  while [[ -z "${value:-}" ]]; do
    read -r -s -p "$label: " value
    echo
  done
  printf '%s' "$value"
}

prompt_default() {
  local label="$1" default="$2" value
  read -r -p "$label [$default]: " value
  printf '%s' "${value:-$default}"
}

railway_token="$(prompt_secret 'Railway Account/API Token را وارد کن')"
export RAILWAY_API_TOKEN="$railway_token"
unset railway_token

repo_input="$(prompt_default 'آدرس GitHub repository' 'https://github.com/USERNAME/REPOSITORY')"
repo="$(normalize_repo "$repo_input")" || fail "آدرس GitHub باید مثل owner/repository یا https://github.com/owner/repository باشد."
branch="$(prompt_default 'نام branch' 'main')"
project_name="$(prompt_default 'نام پروژه Railway' "morself-${repo##*/}")"
service_name="$(prompt_default 'نام سرویس Railway' 'morself-bot')"
workspace=""
read -r -p 'نام یا ID Workspace (اختیاری؛ برای Workspace شخصی خالی بگذار): ' workspace
bot_token="$(prompt_secret 'توکن BotFather را وارد کن')"
owner_id="$(prompt_default 'آیدی عددی مالک تلگرام' '')"
[[ "$owner_id" =~ ^[1-9][0-9]{0,19}$ ]] || fail 'آیدی مالک باید یک عدد مثبت تلگرام باشد.'

cat <<EOF

تنظیمات آماده است:
  GitHub:    $repo ($branch)
  Project:   $project_name
  Service:   $service_name
  Data path: /data
EOF
read -r -p 'ادامه بدهم و منابع Railway را بسازم؟ [y/N] ' confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo 'لغو شد.'; exit 0; }

# Railway CLI stores the project link in the current directory. This file is
# local metadata and does not contain the Railway API token.
init_args=(init --name "$project_name" --json)
if [[ -n "$workspace" ]]; then
  init_args+=(--workspace "$workspace")
fi

echo '۱/۶ ساخت پروژه Railway...'
railway "${init_args[@]}" >/tmp/morself-railway-init.json

echo '۲/۶ ساخت سرویس...'
railway add --service "$service_name" >/tmp/morself-railway-add.log

echo '۳/۶ اتصال repository گیت‌هاب...'
railway service source connect --repo "$repo" --branch "$branch" --service "$service_name"

echo '۴/۶ ثبت Variables محرمانه...'
# Values are passed directly to the CLI and are not echoed by this script.
railway variable set \
  "MOR_BOT_TOKEN=$bot_token" \
  "MOR_OWNER_ID=$owner_id" \
  "MOR_DATA_DIR=/data" \
  --service "$service_name"
unset bot_token owner_id

echo '۵/۶ ساخت Volume و اتصال آن به /data...'
if railway volume add --mount-path /data --service "$service_name"; then
  :
else
  echo 'ساخت Volume ناموفق بود؛ ممکن است این پروژه از قبل Volume داشته باشد.' >&2
  echo 'فهرست Volumeها:' >&2
  railway volume list || true
  exit 1
fi

echo '۶/۶ شروع deployment نهایی...'
railway redeploy --service "$service_name" --yes || railway redeploy --yes

unset RAILWAY_API_TOKEN
cat <<'EOF'

انجام شد.
حالا در تلگرام از حساب مالک /start را برای ربات بفرست و ورود اکانت را انجام بده.
Session و تنظیمات داخل Volume مسیر /data باقی می‌مانند.
برای دیدن لاگ‌ها:
  railway logs --service SERVICE_NAME
EOF
