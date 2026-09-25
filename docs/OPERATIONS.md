# Operations and troubleshooting

## Routine health checks

```bash
systemctl status mksi-varilightd
/opt/mksi-varilightd/healthcheck.sh
journalctl -u mksi-varilightd --since '10 min ago'
```

For the optional UI:

```bash
systemctl status mksi-varilight-ui
curl -s http://127.0.0.1:8088/api/health
curl -s http://127.0.0.1:8088/api/state
```

The controller management API is loopback-only by default:

```bash
curl -s http://127.0.0.1:2017/devices
curl -s http://127.0.0.1:2017/enrollment
```

## Useful controller log events
Useful structured events include:

- `device_registered` and `registration_rejected`;
- enrollment start/candidate/accept and inventory-management events;
- `mqtt_command`;
- `supla_tx` / `supla_rx`;
- state-change origin/brightness/RTT;
- MQTT connect/disconnect/SUBACK validation;
- stale snapshot and session-replacement information.

Routine call-40/call-50 pings occur roughly every five seconds. The web UI filters those plus routine loopback `GET /devices` and `GET /enrollment` polling. Raw `journalctl` may still contain management HTTP polling lines.

## Device stays offline

1. confirm the dimmer joined Wi-Fi;
2. confirm its server address points to the controller;
3. confirm TCP 2016 is listening;
4. inspect the journal for a peer connection;
5. inspect `registration_rejected` reason/GUID;
6. if intentionally new/deleted, open **+ Add Light** and accept it;
7. otherwise verify the device exists in the persistent inventory.

No connection attempt usually means Wi-Fi/routing/setup, not MQTT or Home Assistant.
## Enrollment candidate keeps retrying

A deleted/unknown dimmer may reconnect repeatedly with a different ephemeral TCP source port. Candidate identity is the GUID, not the port. The UI preserves the typed name/focus across those retries.

If no candidate appears, confirm the enrollment window is active and check compatibility rejection reasons. Only registrations matching the tested protocol/channel profile are offered for enrollment.

## Home Assistant device missing

Check in this order:

1. controller is MQTT-ready;
2. retained discovery exists under `homeassistant/light/...` and `homeassistant/sensor/...`;
3. HA broker identity can receive those records;
4. HA can subscribe/receive the MKSI runtime topics;
5. restart/reconnect the controller after ACL changes to republish discovery;
6. reload HA MQTT if needed.

Do not use a failed `mksi/#` subscription as proof that narrow ACLs are broken.

## Unexpected OFF or brightness change

A preceding `mqtt_command` plus call-110 transmit indicates a controller request. A report with no matching pending command is `device-originated`; initial state after reconnect is `reconnect-startup`.
## MQTT failure

Device TLS sessions and MQTT lifecycle are separate. On broker recovery the daemon waits for command-topic SUBACK, then republishes discovery and retained state.

Delete deliberately refuses to proceed when MQTT is unavailable because retained discovery/state cannot then be cleared safely.

## Safe rollback

Keep the previous known-good server/address while evaluating. A dimmer can be returned by entering setup mode, restoring the previous server address and rebooting.

Do not remove the old path until migrated devices pass physical control, remote control, disconnect/reconnect and power-cycle tests.
