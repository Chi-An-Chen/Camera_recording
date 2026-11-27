#!/bin/bash
# sudo bash install_service.sh [all|web_viewer|record_cams]
set -eu
case "${1:-all}" in
  all) services=(record_cams web_viewer) ;;
  web_viewer|record_cams) services=("$1") ;;
  *) echo "Usage: $0 [all|web_viewer|record_cams]" >&2; exit 2 ;;
esac
DIR="$(cd "$(dirname "$0")" && pwd -P)"
service_user=${SERVICE_USER:-${SUDO_USER:-}}
if [[ -z "$service_user" ]] || ! id "$service_user" >/dev/null 2>&1; then
  echo "請使用 sudo 執行，或用 SERVICE_USER 指定現有的服務帳號。" >&2
  exit 2
fi
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
# Render the units together before installing either one.
python3 - "$DIR" "$service_user" "$staging" "${services[@]}" <<'PY'
from pathlib import Path
import sys

directory, user, staging, *services = sys.argv[1:]
if any(ord(c) < 32 for c in directory + user):
    sys.exit("Invalid control character in service path or account")
path = directory.replace("%", "%%")
quoted = '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
values = {"RequiresMountsFor=@QUOTED_PROJECT_DIR@": "RequiresMountsFor=" + quoted,
          "WorkingDirectory=@PROJECT_DIR@": "WorkingDirectory=" + path,
          "User=@SERVICE_USER@": "User=" + user}
for service in services:
    name = service + ".service"
    lines = (Path(directory) / name).read_text().splitlines()
    (Path(staging) / name).write_text("\n".join(values.get(line, line) for line in lines) + "\n")
PY
for svc in "${services[@]}"; do
  install -m 0644 "$staging/$svc.service" "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload
for svc in "${services[@]}"; do
  systemctl enable "$svc.service"
  systemctl restart "$svc.service" || { systemctl --no-pager status "$svc.service"; exit 1; }
done
sleep 3
for svc in "${services[@]}"; do
  if ! systemctl is-active --quiet "$svc.service"; then
    systemctl --no-pager status "$svc.service" || true
    exit 1
  fi
  echo "$svc.service active"
done
if [[ "${1:-all}" != record_cams ]]; then
  echo "網頁: http://$(hostname -I | awk '{print $1}'):8080"
fi
