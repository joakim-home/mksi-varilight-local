#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-paho-mqtt ca-certificates

if ! id mksi-varilight-ui >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin mksi-varilight-ui
fi
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -a -G systemd-journal mksi-varilight-ui
fi

install -d -o root -g root -m 0755 /opt/mksi-varilight-ui /opt/mksi-varilight-ui/www
install -d -o root -g mksi-varilight-ui -m 0750 /etc/mksi-varilight-ui
install -o root -g root -m 0644 "$ROOT_DIR/ui.py" /opt/mksi-varilight-ui/ui.py
for f in index.html app.js style.css; do
  install -o root -g root -m 0644 "$ROOT_DIR/www/$f" "/opt/mksi-varilight-ui/www/$f"
done
install -o root -g root -m 0644 "$ROOT_DIR/systemd/mksi-varilight-ui.service" /etc/systemd/system/mksi-varilight-ui.service

if [ ! -f /etc/mksi-varilight-ui/config.json ]; then
  install -o root -g mksi-varilight-ui -m 0640 "$ROOT_DIR/examples/ui.config.json" /etc/mksi-varilight-ui/config.json
  echo "Installed example config; edit /etc/mksi-varilight-ui/config.json."
fi
[ -s /etc/mksi-varilight-ui/mqtt.password ] || {
  echo "missing /etc/mksi-varilight-ui/mqtt.password" >&2
  echo "see docs/INSTALLATION.md" >&2
  exit 1
}

chown root:mksi-varilight-ui /etc/mksi-varilight-ui/config.json /etc/mksi-varilight-ui/mqtt.password
chmod 0640 /etc/mksi-varilight-ui/config.json /etc/mksi-varilight-ui/mqtt.password
python3 -m py_compile /opt/mksi-varilight-ui/ui.py
systemctl daemon-reload
systemctl enable mksi-varilight-ui.service
systemctl restart mksi-varilight-ui.service
systemctl --no-pager --full status mksi-varilight-ui.service
