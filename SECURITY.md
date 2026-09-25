# Security

## Intended deployment boundary

MKSI Varilight Local is designed for a trusted home/lab LAN or isolated IoT VLAN. Do **not** expose TCP/2016, the MQTT broker, the management endpoint or the web UI directly to the public Internet.

## Legacy TLS is intentional

The tested stock dimmer firmware requires TLS 1.1 and `AES128-SHA`. `controller.py` deliberately lowers the OpenSSL security level for that listener for compatibility.

This is not modern transport security. Network segmentation and access control are the primary security boundary.

## Web UI authority

The bundled UI:

- serves plain HTTP by default;
- has no authentication/authorization layer;
- can publish light commands;
- can **Add/Enroll, Rename and Delete** controller inventory entries;
- can expose controller/UI journal output when its service user is in `systemd-journal`.

Anyone who can reach the UI should be treated as an administrator of these lights.
Bind/firewall the UI to trusted clients only. For remote access, use an authenticated TLS reverse proxy or VPN rather than port-forwarding 8088.

## Management API

The controller management API defaults to `127.0.0.1:2017`. Keep it loopback-only. The UI proxies management actions locally so it does not need root access or direct write permission to `/etc`.

Do not expose the management port merely to support a UI on another host; design an authenticated/authorized transport first.

## Enrollment

Unknown GUIDs remain rejected unless a user explicitly opens a time-limited enrollment window. Compatibility checks are applied before a candidate is offered, and acceptance is explicit.

This protects against accidental enrollment; it is not a substitute for network access control or UI authentication.

## MQTT

Use separate credentials for controller, UI and Home Assistant. Apply least-privilege ACLs from `docs/MQTT.md`. Avoid broad `#` permissions and anonymous Internet-reachable MQTT.
## Secrets

The repository ignores common private material including `*.key`, `*.crt`, `*.pem`, `*.password`, local config files and `.env` files.

Example JSON contains fabricated addresses only. Never replace examples with a live configuration and commit without review.

## Reporting a vulnerability

Prefer a private GitHub security advisory when available. This independent hobby/community project carries no security support commitment from VARILIGHT, SUPLA or Zamel.
