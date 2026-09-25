#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
START=0
[ "${1:-}" = "--start" ] && START=1
[ -z "${1:-}" ] || [ "$START" -eq 1 ] || { echo "usage: $0 [--start]" >&2; exit 2; }

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-paho-mqtt ca-certificates iproute2

if ! id mksi-varilightd >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin mksi-varilightd
fi

install -d -o root -g root -m 0755 /opt/mksi-varilightd
install -d -o root -g mksi-varilightd -m 0750 /etc/mksi-varilightd
for f in controller.py protocol.py wait-for-bind.py; do
  install -o root -g root -m 0644 "$ROOT_DIR/$f" "/opt/mksi-varilightd/$f"
done
install -o root -g root -m 0755 "$ROOT_DIR/scripts/healthcheck.sh" /opt/mksi-varilightd/healthcheck.sh
install -o root -g root -m 0644 "$ROOT_DIR/systemd/mksi-varilightd.service" /etc/systemd/system/mksi-varilightd.service

if [ ! -f /etc/mksi-varilightd/config.json ]; then
  install -o root -g mksi-varilightd -m 0640 "$ROOT_DIR/examples/controller.config.json" /etc/mksi-varilightd/config.json
  echo "Installed example config; edit /etc/mksi-varilightd/config.json before starting."
fi

for secret in server.crt server.key mqtt.password; do
  [ -s "/etc/mksi-varilightd/$secret" ] || {
    echo "missing /etc/mksi-varilightd/$secret" >&2
    echo "see docs/INSTALLATION.md" >&2
    exit 1
  }
done

chown root:mksi-varilightd /etc/mksi-varilightd/config.json /etc/mksi-varilightd/server.crt /etc/mksi-varilightd/server.key /etc/mksi-varilightd/mqtt.password
chmod 0640 /etc/mksi-varilightd/config.json /etc/mksi-varilightd/server.crt /etc/mksi-varilightd/server.key /etc/mksi-varilightd/mqtt.password
python3 -m py_compile /opt/mksi-varilightd/controller.py /opt/mksi-varilightd/protocol.py /opt/mksi-varilightd/wait-for-bind.py
systemctl daemon-reload

if [ "$START" -eq 1 ]; then
  systemctl enable mksi-varilightd.service
  systemctl restart mksi-varilightd.service
  systemctl --no-pager --full status mksi-varilightd.service
else
  echo "Installed but not started. Review config, then run: systemctl enable --now mksi-varilightd"
fi
