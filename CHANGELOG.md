# Changelog

## Unreleased - 2026-10-01

- Synced deployed TLS 1.2/ECDHE and registration-G/protocol-25 support for firmware 26.08.
- Retained TLS 1.1, E/version-12 registration and enrollment/layout checks.
- Added nine regression tests: total 64, including actual old/new TLS handshakes.
- Preserved generic repository bind default instead of copying the deployment address.
- Updated compatibility/security documentation. Publication remains on hold for manufacturer review.
- No claim that spontaneous-off behaviour or LivingRoom Alexa acceptance is resolved.

## 0.2.0 - 2026-09-25

Device-management and Home Assistant hardening release.

- Persistent controller-owned inventory under the systemd state directory.
- Zero-device first start; no fabricated placeholder GUID required.
- Five-minute explicit **Add/Enroll** flow for compatible unknown devices.
- **Rename** preserves GUID/internal/MQTT/Home Assistant identity.
- **Delete** clears retained state + HA discovery before forgetting a device.
- Deleted devices can be re-enrolled without factory reset when network/server config is unchanged.
- Loopback-only management API; UI does not need root/file-write access.
- Stable enrollment UI keyed by GUID across changing reconnect source ports.
- Routine ping and management polling filtered from the web Logs view.
- Documented least-privilege Home Assistant/Mosquitto Dynamic Security ACL requirements.
- Confirmed HA discovery, dual availability and disconnected-device unavailable behavior.
- Test suite increased to **55 tests** (36 controller + 19 protocol), plus syntax/config checks.

## 0.1.0 - 2026-09-25

First GitHub-ready working beta.
- Direct stock-firmware VARILIGHT V-Pro Smart controller on TLS/2016.
- Confirmed firmware 2.8.55 / SUPLA protocol-v12 behavior.
- Multi-device concurrent sessions proven with three dimmers.
- MQTT state, command and daemon/device availability contract.
- Home Assistant MQTT discovery for light + diagnostic entities.
- Physical/device-originated state reporting and commanded-state correlation.
- Reconnect-safe desired command queue and stale retained-state protection.
- Local MKSI web UI with Controls and Logs tabs.
- UI ON/OFF state indication and filtered routine ping chatter.
- Debian/systemd install helpers and health check.
- Confirmed two-consecutive-reset procedure for fully clearing tested dimmer configuration.
