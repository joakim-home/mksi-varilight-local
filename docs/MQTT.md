# MQTT contract

MQTT is the stable integration boundary between the direct dimmer controller and higher-level automation clients.

`<name>` is the controller-owned stable device key, for example `livingroom`. Rename changes the display name, not this key or the GUID-derived Home Assistant unique ID.

## Controller topics

| Topic | Direction | Retained | Meaning |
|---|---|---|---|
| `mksi/varilightd/availability` | controller -> broker | yes/LWT | daemon online/offline |
| `mksi/lights/<name>/availability` | controller -> broker | yes | device session availability |
| `mksi/lights/<name>/state/on` | controller -> broker | yes | `ON` or `OFF` |
| `mksi/lights/<name>/state/brightness` | controller -> broker | yes | integer `0..100` |
| `mksi/lights/<name>/diagnostic/origin` | controller -> broker | yes | change origin |
| `mksi/lights/<name>/diagnostic/last_change` | controller -> broker | yes | ISO-8601 timestamp |
| `mksi/lights/<name>/command/on` | broker -> controller | no | `ON` / `OFF` |
| `mksi/lights/<name>/command/brightness` | broker -> controller | no | target `0..100` |

All controller publications use QoS 1. Commands must never be retained.

## Home Assistant discovery
The controller publishes retained discovery documents under:

```text
homeassistant/light/mksi_varilight_<guidhex>/config
homeassistant/sensor/mksi_varilight_<guidhex>_origin/config
homeassistant/sensor/mksi_varilight_<guidhex>_last_change/config
```

Discovery uses daemon + device availability with `availability_mode: all`.

Delete publishes empty retained payloads to the device discovery/state topics before removing the device from inventory. Re-enrollment republishes fresh discovery and state.

## Suggested broker permissions

Use separate credentials for controller, UI and Home Assistant.

Controller identity needs:

```text
SUBSCRIBE mksi/lights/+/command/+
PUBLISH   mksi/varilightd/availability
PUBLISH   mksi/lights/+/availability
PUBLISH   mksi/lights/+/state/+
PUBLISH   mksi/lights/+/diagnostic/+
PUBLISH   homeassistant/light/+/config
PUBLISH   homeassistant/sensor/+/config
```
UI identity needs:

```text
SUBSCRIBE mksi/varilightd/availability
SUBSCRIBE mksi/lights/+/availability
SUBSCRIBE mksi/lights/+/state/+
SUBSCRIBE mksi/lights/+/diagnostic/+
PUBLISH   mksi/lights/+/command/+
```

Home Assistant needs command publish plus subscribe/receive access to the MKSI state, availability and diagnostic topics, and receive access to the retained `homeassistant/light/+/config` and `homeassistant/sensor/+/config` records. HA must also be permitted to subscribe to its configured discovery prefix, commonly `homeassistant/#`.

For Mosquitto Dynamic Security, remember that `subscribePattern`, `publishClientReceive` and `publishClientSend` are separate ACL types. See `docs/HOME_ASSISTANT.md` for the practical least-privilege pattern.

Translate these permissions into your broker's model rather than granting a broad `#` wildcard.

## Readiness and retained-state semantics

The daemon waits for a successful command-topic SUBACK before declaring MQTT ready. It then publishes daemon `online`, discovery and current retained state.

State snapshots carry a revision number. Delayed snapshots are dropped rather than being allowed to overwrite newer retained values.

On graceful stop the controller publishes retained daemon `offline` before disconnecting. Unexpected loss is covered by the broker Last Will.
