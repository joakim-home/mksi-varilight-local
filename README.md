# MKSI Varilight Local

**Local, self-hosted control for compatible VARILIGHT V-Pro Smart dimmers — using the stock firmware.**

MKSI Varilight Local replaces the server side of the normal SUPLA deployment with a small local controller that talks directly to the dimmers and exposes them through MQTT, Home Assistant and a lightweight web UI.

The goal is deliberately simple: **keep the Varilight hardware and firmware, keep control local, and avoid running a much larger software stack just to operate a few dimmers.**

> **Status:** working beta. Tested with **3 × VARILIGHT V-Pro Smart** dimmers running firmware **2.8.55** / SUPLA protocol **v12**.

> **1 October checkpoint:** Publication is on hold for manufacturer review. The replacement firmware 26.08 required live TLS 1.2 and registration-G/protocol-25 patches that are **not yet incorporated in this repository**. See the [September work record](docs/SEPTEMBER_2026_CHECKPOINT.md) for the changes, Alexa status, security findings and remaining validation.

## Why this exists

This started as troubleshooting a local-control problem and turned into an interoperability project.

The tested dimmers already contain perfectly usable stock firmware, but the normal server path brings in considerably more infrastructure than was useful for a small home-lab deployment. MKSI Varilight Local implements only the observed protocol behaviour needed by the dimmers and keeps the rest of the stack small.

No dimmer firmware modification is required.
## What you get

- Direct local control of compatible V-Pro Smart dimmers.
- ON/OFF and literal `0..100` brightness control.
- Physical dimmer changes reported back immediately.
- MQTT state, commands and availability.
- Home Assistant MQTT discovery — no custom HA integration required.
- Local web UI with live state, controls and filtered logs.
- **Add / Enroll, Rename, Delete and Re-add** from the UI.
- Stable GUID-based identity across friendly-name changes.
- Per-device and daemon availability handling.
- Reconnect-safe command/state handling and retained-state protection.
- No `supla-server`, MariaDB, PHP, nginx or SUPLA Cloud in the runtime path.

## Architecture

```text
VARILIGHT V-Pro Smart (stock firmware)
        |
        | TLS 1.1 / AES128-SHA, TCP 2016
        | observed SUPLA protocol-v12 subset
        v
  mksi-varilightd
        |
        +------ MQTT ------> Home Assistant / automations
        |
        +------ loopback management API
                         ^
                         |
                  MKSI local web UI
```
## Quick start

1. Provide a small Linux host/container with a fixed LAN address.
2. Provide an MQTT broker and create least-privilege controller/UI credentials.
3. Copy the example configuration files and set the controller/broker addresses.
4. Create the local TLS certificate/key and MQTT password files.
5. Install and start `mksi-varilightd` and, optionally, the local UI.
6. Put one dimmer into setup mode and point its server field at the controller host.
7. Open the UI, click **+ Add Light**, and start the five-minute enrollment window.
8. When the compatible unknown device appears, give it a friendly name and add it.
9. Validate physical control, UI/MQTT control and Home Assistant discovery before moving the next dimmer.

Full instructions are in [Installation](docs/INSTALLATION.md) and [Device setup](docs/DEVICE_SETUP.md).

On the tested hardware the setup page is available at `http://192.168.4.1/`. A complete configuration clear required the normal reset sequence **twice consecutively**.

## Home Assistant

The controller publishes standard MQTT discovery documents for each light plus diagnostic sensors. Home Assistant therefore needs only its normal MQTT integration connected to the same broker.

Each light uses **both** controller and device availability, so a stale retained `online` value cannot make a disconnected light look healthy.

See [Home Assistant](docs/HOME_ASSISTANT.md) for discovery behaviour and the broker ACLs required when using Mosquitto Dynamic Security or another least-privilege setup.
## Compatibility

Confirmed on the reference hardware:

- VARILIGHT V-Pro Smart
- firmware `2.8.55`
- SUPLA protocol `v12`
- one channel, channel `0`, type `4000`
- TLS 1.1 with `AES128-SHA`
- literal brightness values `0..100`

The architecture is not limited to three devices, but **compatibility beyond the tested hardware/firmware is not yet claimed**. Other revisions may behave differently.

See [Protocol notes](docs/PROTOCOL.md) for the confirmed wire behaviour.

## Security

The stock dimmer requires legacy TLS 1.1 and a legacy cipher suite. The bundled web UI is intentionally small and LAN-focused: it serves HTTP and does not provide its own authentication layer.

The UI can control lights and manage the device inventory, so deploy it only on a trusted LAN/VLAN or place authenticated access in front of it. The controller management endpoint binds to loopback and should remain private.

See [SECURITY.md](SECURITY.md) before exposing any part of the system beyond a trusted network.

## Tests

```bash
python3 -m unittest -v test_protocol.py test_controller.py
python3 -m py_compile controller.py protocol.py ui.py wait-for-bind.py
node --check www/app.js
```

Current baseline: **55 tests** — 36 controller/lifecycle/MQTT/device-management tests and 19 protocol tests.
## Documentation

- [Installation](docs/INSTALLATION.md) — host, TLS, MQTT, systemd and first start.
- [Device setup](docs/DEVICE_SETUP.md) — reset/AP flow, enrollment and migration.
- [Architecture](docs/ARCHITECTURE.md) — state ownership, sessions and management model.
- [Protocol](docs/PROTOCOL.md) — confirmed calls, payloads and framing.
- [MQTT](docs/MQTT.md) — topics, retained state and least-privilege ACL guidance.
- [Home Assistant](docs/HOME_ASSISTANT.md) — discovery, availability and validation.
- [Operations](docs/OPERATIONS.md) — health checks, logs, recovery and rollback.
- [Security](SECURITY.md) — deployment boundary and legacy TLS considerations.

## Repository layout

```text
controller.py / protocol.py    local dimmer controller
ui.py / www/                   optional local UI + device management
examples/                      generic zero-device configuration
scripts/                       installation and health-check helpers
systemd/                       service units
docs/                          setup, protocol, HA and operations docs
test_*.py                      offline test suite
```

## Project scope

MKSI Varilight Local is an independent interoperability/community project. It is **not affiliated with, endorsed by, or supported by VARILIGHT, SUPLA or Zamel**. Product and project names belong to their respective owners.

The project contains its own controller/UI code and documentation. It does not distribute VARILIGHT or SUPLA firmware or proprietary source code.

Licensed under the [MIT License](LICENSE).
