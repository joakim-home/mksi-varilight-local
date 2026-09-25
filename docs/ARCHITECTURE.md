# Architecture

## Design goal

MKSI Varilight Local replaces the server side of the stock VARILIGHT V-Pro Smart/SUPLA deployment without modifying the dimmer firmware.

The controller intentionally implements a small, observed protocol surface instead of cloning the complete SUPLA platform.

```text
+----------------------+        +--------------------+
| VARILIGHT V-Pro      | TLS    | mksi-varilightd    |
| Smart stock firmware |------->| protocol + state   |
+----------------------+ :2016  +----+----------+----+
                                      |          |
                                  MQTT|          |loopback HTTP
                                      v          v
                              Home Assistant   MKSI UI
```

## Components

### `controller.py`

Owns the TLS listener, sessions, persistent device inventory, MQTT lifecycle, command queues, Home Assistant discovery and the loopback management API.
### `protocol.py`

Contains packed SUPLA framing and only the call/payload codecs needed by the tested dimmer firmware. It has no MQTT, UI or deployment knowledge.

### MQTT broker

Acts as the stable integration boundary. The controller publishes retained state/availability and Home Assistant discovery, and consumes non-retained command topics.

### `ui.py` + `www/`

Optional LAN-only UI. Light state/control uses MQTT. Inventory management is proxied to the controller's loopback-only HTTP API. The UI can also show a filtered, read-only view of the controller/UI systemd journals.

## Identity and persistent inventory

The permanent physical identity is the dimmer's 16-byte GUID. Each accepted device also has:

- a stable internal/MQTT `name`;
- a mutable `display_name` shown to humans;
- its immutable GUID.

The runtime inventory is persisted in `/var/lib/mksi-varilightd/devices.json` by default. When present, it overrides the static `devices` seed in the controller config. This allows a clean install to start with `devices: []` and be populated through enrollment.
## Enrollment and management

Unknown GUIDs are rejected by default. Clicking **+ Add Light** opens a five-minute enrollment window. During that window, a compatible unknown registration is recorded as a candidate but is still rejected until the user explicitly accepts it.

Acceptance validates the candidate, creates a stable internal name, persists the inventory and restarts the controller. The dimmer's normal reconnect then succeeds and discovery/state are published.

Rename changes only `display_name`; GUID and internal identity remain stable. Delete requires GUID confirmation, clears retained MQTT state and Home Assistant discovery, persists the reduced inventory, then restarts. A deleted physical dimmer is not factory-reset and can be enrolled again later.

The management listener defaults to `127.0.0.1:2017`. It is deliberately separate from the public LAN UI listener so the UI service does not need root or direct write access to controller configuration.

## Session and state model

Each device has one current session. Session replacement is identity-safe: an old worker cannot detach or overwrite a newer reconnect.

Desired commands belong to stable device state, not to a socket. The TLS session thread performs device writes; MQTT callbacks only validate and enqueue commands.
Per-device state tracks brightness, last non-zero brightness, connected/session state, origin/timestamp, reconnect/change counters, pending command timing and a monotonically increasing revision.

State publication is revision-checked so a delayed snapshot cannot overwrite newer retained MQTT state.

## Availability and failure behavior

The controller publishes retained daemon availability at `mksi/varilightd/availability` plus retained per-device availability. Home Assistant discovery uses both with `availability_mode: all`.

On clean shutdown the daemon explicitly publishes `offline`; unexpected loss is covered by MQTT Last Will. When a physical dimmer disconnects, its last known state remains visible but its per-device availability becomes `offline` and the UI disables controls.

## ON/OFF semantics

Brightness is canonical. OFF sends `0`. ON restores the controller-owned last non-zero brightness, defaulting to `100` if none has been observed.

Physical reports are tagged `device-originated`; correlated controller commands are `commanded`; initial state after reconnect is `reconnect-startup`.

## Deliberate exclusions

The runtime does not require `supla-server`, MariaDB, PHP, nginx, SUPLA Cloud, SUPLA MQTT discovery or `local/supla/#` topics.
