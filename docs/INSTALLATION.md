# Installation

The reference deployment is a small Debian-family Linux host/container with a fixed LAN address. The dimmers must reach TCP 2016 on the controller host.

## Prerequisites

- Python 3.10+ recommended;
- `paho-mqtt` 2.x;
- MQTT broker reachable from controller, UI and Home Assistant as applicable;
- fixed/reserved LAN address for the controller;
- TCP 2016 from the dimmer network to the controller;
- optional TCP 8088 from trusted clients to the UI.

The included scripts target Debian/Ubuntu and use system packages.

## Prepare configuration

```bash
sudo mkdir -p /etc/mksi-varilightd /etc/mksi-varilight-ui
sudo cp examples/controller.config.json /etc/mksi-varilightd/config.json
sudo cp examples/ui.config.json /etc/mksi-varilight-ui/config.json
sudo editor /etc/mksi-varilightd/config.json
sudo editor /etc/mksi-varilight-ui/config.json
```
The examples intentionally start with `"devices": []`. The persistent inventory is populated through the UI enrollment flow.

`inventory_file` defaults to `/var/lib/mksi-varilightd/devices.json`. The systemd unit uses `StateDirectory=mksi-varilightd`, so the directory is created for the controller service account.

The management API defaults to `127.0.0.1:2017`, and the UI defaults to that loopback URL. Keep controller and UI on the same host for Add/Rename/Delete. If you intentionally separate them, MQTT light control still works, but inventory management needs a separately secured design; do not simply expose port 2017.

## TLS certificate

The tested dimmer requires legacy TLS 1.1. A local self-signed RSA certificate is sufficient for the reference deployment:

```bash
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/mksi-varilightd/server.key \
  -out /etc/mksi-varilightd/server.crt \
  -subj '/CN=mksi-varilightd'
```

The controller explicitly configures `TLSv1_1` and `AES128-SHA:@SECLEVEL=0`; see `SECURITY.md`.
## MQTT credentials and ACLs

Create separate broker identities for controller and UI and store only the password locally:

```bash
sudo sh -c 'umask 077; printf "%s\n" "CONTROLLER_PASSWORD" > /etc/mksi-varilightd/mqtt.password'
sudo sh -c 'umask 077; printf "%s\n" "UI_PASSWORD" > /etc/mksi-varilight-ui/mqtt.password'
```

Configure broker ACLs before expecting discovery/control to work. See `docs/MQTT.md` and `docs/HOME_ASSISTANT.md`.

## Install the controller

```bash
sudo ./scripts/install-controller.sh
sudo systemctl enable --now mksi-varilightd
sudo /opt/mksi-varilightd/healthcheck.sh
sudo journalctl -u mksi-varilightd -f
```

A healthy daemon listens on configured TCP/2016, starts the loopback management endpoint and publishes `mksi/varilightd/availability=online` after MQTT subscription readiness.
## Install the optional UI

```bash
sudo ./scripts/install-ui.sh
```

The default UI listens on `0.0.0.0:8088`. Browse to `http://CONTROLLER-IP:8088/` from a trusted LAN client.

The installer adds the UI account to `systemd-journal` when available, enabling the read-only Logs tab. Remove that membership if journal visibility is not wanted.

## First device

Follow `docs/DEVICE_SETUP.md`: point one dimmer at the controller, click **+ Add Light**, accept/name the compatible GUID, validate physical + remote control and reconnect behavior, then migrate additional devices one at a time.

## Offline verification before installation

```bash
python3 -m unittest -v test_protocol.py test_controller.py
python3 -m py_compile controller.py protocol.py ui.py wait-for-bind.py
node --check www/app.js
```

Do not skip this after changing protocol, lifecycle, MQTT, management or session code.
