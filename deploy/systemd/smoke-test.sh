#!/usr/bin/env bash
set -euo pipefail

SOURCE_REPOSITORY="${1:-$(pwd)}"
REVISION="${2:-HEAD}"
INSTALL_ROOT="/opt/bidpilot"
ENVIRONMENT_ROOT="/etc/bidpilot"
WEB_UNIT="/etc/systemd/system/bidpilot-web.service"
WORKER_UNIT="/etc/systemd/system/bidpilot-worker.service"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "systemd smoke test must run as root" >&2
    exit 2
  fi
}

assert_unused_path() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    echo "refusing to overwrite existing path: ${path}" >&2
    exit 3
  fi
}

safe_remove_tree() {
  local expected="$1"
  local resolved
  resolved="$(readlink -m -- "${expected}")"
  if [[ "${resolved}" != "${expected}" ]]; then
    echo "refusing to remove unexpected path: ${resolved}" >&2
    return 1
  fi
  rm -rf -- "${resolved}"
}

cleanup() {
  systemctl stop bidpilot-worker.service bidpilot-web.service >/dev/null 2>&1 || true
  rm -f -- "${WEB_UNIT}" "${WORKER_UNIT}"
  systemctl daemon-reload >/dev/null 2>&1 || true
  safe_remove_tree "${INSTALL_ROOT}" || true
  safe_remove_tree "${ENVIRONMENT_ROOT}" || true
  if id bidpilot >/dev/null 2>&1; then
    userdel bidpilot >/dev/null 2>&1 || true
  fi
}

require_root
assert_unused_path "${INSTALL_ROOT}"
assert_unused_path "${ENVIRONMENT_ROOT}"
assert_unused_path "${WEB_UNIT}"
assert_unused_path "${WORKER_UNIT}"
trap cleanup EXIT

useradd --system --home "${INSTALL_ROOT}" --shell /usr/sbin/nologin bidpilot
git clone -q --no-local "${SOURCE_REPOSITORY}" "${INSTALL_ROOT}"
git -C "${INSTALL_ROOT}" checkout -q "${REVISION}"
python3 -m venv "${INSTALL_ROOT}/.venv"
"${INSTALL_ROOT}/.venv/bin/python" -m pip install -q --upgrade pip
"${INSTALL_ROOT}/.venv/bin/python" -m pip install -q -e "${INSTALL_ROOT}"

install -d -o bidpilot -g bidpilot -m 0700 \
  "${INSTALL_ROOT}/data" "${INSTALL_ROOT}/outputs/reports"
install -d -o root -g bidpilot -m 0750 "${ENVIRONMENT_ROOT}"
install -o root -g bidpilot -m 0640 \
  "${INSTALL_ROOT}/deploy/systemd/bidpilot.env.example" \
  "${ENVIRONMENT_ROOT}/bidpilot.env"
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/systemd/bidpilot-web.service" "${WEB_UNIT}"
install -o root -g root -m 0644 \
  "${INSTALL_ROOT}/deploy/systemd/bidpilot-worker.service" "${WORKER_UNIT}"

systemctl daemon-reload
systemctl start bidpilot-web.service
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/tmp/bidpilot-health.json 2>/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health
systemctl start bidpilot-worker.service
systemctl is-active bidpilot-web.service bidpilot-worker.service
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/api/v1/system/status \
    >/tmp/bidpilot-system-status.json 2>/dev/null \
    && python3 -c \
      'import json; assert json.load(open("/tmp/bidpilot-system-status.json"))["worker_online"]' \
      2>/dev/null; then
    break
  fi
  sleep 1
done
python3 -c \
  'import json; assert json.load(open("/tmp/bidpilot-system-status.json"))["worker_online"]'
runuser -u bidpilot -- sh -c \
  "cd '${INSTALL_ROOT}' && exec '${INSTALL_ROOT}/.venv/bin/python' -m bidpilot status"
systemctl show bidpilot-web.service \
  -p MainPID -p ActiveState -p SubState -p User -p Group --no-pager

systemctl stop bidpilot-worker.service bidpilot-web.service
if systemctl is-active --quiet bidpilot-web.service; then
  echo "web service did not stop" >&2
  exit 4
fi
if systemctl is-active --quiet bidpilot-worker.service; then
  echo "worker service did not stop" >&2
  exit 4
fi
echo "systemd smoke test passed"
