# Home Assistant

MKSI Varilight Local uses standard Home Assistant MQTT discovery. No custom Home Assistant integration is required.

## Prerequisites

- Home Assistant MQTT integration connected to the same broker;
- MQTT discovery enabled (default prefix `homeassistant`);
- broker ACLs that let Home Assistant receive MKSI discovery/state/availability and publish MKSI command topics.

The controller republishes discovery whenever its MQTT session becomes ready, including after controller restart/reconnect.

## Entities

Each enrolled dimmer publishes one MQTT `light` entity with ON/OFF, `0..100` brightness, device information and dual availability.

Two diagnostic sensors are also published:

- **Last change origin** — `commanded`, `device-originated` or `reconnect-startup`;
- **Last change** — timestamp of the most recent accepted state update.

Unique IDs are derived from the dimmer GUID, so display-name changes do not create a new HA device identity.
## Availability

The light is available only when both are true:

1. `mksi/varilightd/availability` is `online`;
2. `mksi/lights/<name>/availability` is `online`.

This is deliberate. A disconnected dimmer should not appear controllable just because the daemon is healthy, and a dead daemon should not look healthy because of a stale retained device value.

## Least-privilege broker access

Home Assistant needs to **publish**:

```text
mksi/lights/+/command/+
```

It needs to **receive/subscribe to matching state topics**:

```text
mksi/lights/+/state/+
mksi/lights/+/availability
mksi/lights/+/diagnostic/+
mksi/varilightd/availability
```
It also needs to receive the retained discovery documents:

```text
homeassistant/light/+/config
homeassistant/sensor/+/config
```

With Mosquitto Dynamic Security, distinguish **subscription permission** from **message receive permission**. Home Assistant normally subscribes to its configured discovery prefix (commonly `homeassistant/#`), so that subscription filter must already be allowed or explicitly allowed. The Varilight role can then grant `publishClientReceive` for the narrower discovery records above.

For MKSI runtime topics, a typical least-privilege role grants `publishClientSend` on commands and both `subscribePattern` + `publishClientReceive` on state/availability/diagnostic topics.

Do not diagnose a narrow ACL by subscribing to `mksi/#`: a correctly restricted account may be denied that broad filter even though all entity topics work. If your broker has multiple roles, also check role/ACL priority for a broad deny that could shadow MKSI allows.

## Discovery after ACL changes

If HA permissions are added after the controller is already connected, restart/reconnect `mksi-varilightd` so it republishes retained discovery, then reload the Home Assistant MQTT integration if needed.
## Validation checklist

For each migrated dimmer:

1. the MQTT device and three entities appear;
2. connected dimmer = available; disconnected dimmer = unavailable;
3. HA OFF switches the real light off;
4. HA ON restores a sensible non-zero brightness;
5. HA brightness changes the real light;
6. physical knob changes update HA without polling;
7. controller restart re-establishes discovery/availability;
8. dimmer power-cycle reconnects and state converges.

Delete from MKSI clears retained discovery, so HA should remove the corresponding MQTT device/entities. Re-enrolling the same GUID recreates the same GUID-derived identity.

## Voice assistants and recorder

There is no direct Alexa/Google implementation. Expose the discovered MKSI lights through your existing Home Assistant voice-assistant path.

Raw protocol traffic is intentionally not represented as HA entities; detailed session logs remain in the controller journal/UI Logs tab.
