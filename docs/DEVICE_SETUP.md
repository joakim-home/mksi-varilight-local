# Dimmer setup, enrollment and migration

Migrate one dimmer at a time and keep the old server available until the new path is proven.

## Confirmed factory-clear behavior

On the tested V-Pro Smart hardware, one normal reset did not always clear all stored configuration. The reliable full-clear procedure was:

1. perform the normal manufacturer reset sequence;
2. after it completes, perform the same reset sequence again;
3. reconnect to the newly created setup AP and configure from scratch.

Use the appropriate manufacturer procedure for physically initiating a reset on your hardware revision.

## Setup access point

After reset/setup mode the dimmer advertises an open SSID similar to:

```text
SUPLA-XXXXXXXXXXXX
```

The tested AP used `192.168.4.0/24`, gateway/web UI `192.168.4.1`.
If a desktop browser does not render the setup page, verify the endpoint directly:

```bash
curl -v http://192.168.4.1/
```

HTTP 200 with HTML proves the setup server is alive; a phone browser may work more reliably around captive-portal/HTTPS-only behavior.

## Configure the dimmer

In the setup page:

1. select the normal 2.4 GHz home/IoT Wi-Fi;
2. enter its Wi-Fi password;
3. set the SUPLA/server address to the **IP address of the host running `mksi-varilightd`**;
4. preserve other required stock fields unless you deliberately know they are unused;
5. save/apply and allow the dimmer to reboot.

Do not add `http://`, `https://` or `:2016` unless your firmware form explicitly requests a port. The tested firmware connects to TCP 2016 itself.

## Add / enroll from the UI

The recommended first-install flow no longer requires a known GUID or placeholder device.
1. Open the MKSI Varilight UI.
2. Click **+ Add Light**. Enrollment is open for five minutes.
3. Wait for the unknown compatible dimmer GUID to appear.
4. Enter a friendly display name and click **Add**.
5. The controller persists the device and restarts itself.
6. The dimmer's normal retry/reconnect should then register successfully and appear online.

Candidates are keyed by GUID, not by TCP source port, so repeated reconnect attempts do not replace the name field while you type.

Enrollment remains explicit: unknown devices are still rejected until the user accepts one. Compatibility checks for protocol version, channel count, channel number/type and brightness layout run before an unknown device is offered as a candidate.

## Rename, delete and re-add

**Rename** changes the display name only. The GUID, internal device key, MQTT identity and Home Assistant unique ID stay stable.

**Delete** clears retained Home Assistant discovery and retained device state before removing the device from the persistent inventory. Delete does **not** factory-reset or reconfigure the physical dimmer; it will be rejected as unknown until enrolled again.

To re-add a deleted dimmer that is still pointed at MKSI, simply open **+ Add Light** and wait for its next retry. A physical reset is only needed when its Wi-Fi/server configuration itself must change.
## Acceptance test for each dimmer

Do not migrate the next device until these pass:

- controller/UI shows the device online;
- physical on/off or dimming updates MQTT/UI/Home Assistant;
- UI or Home Assistant ON/OFF controls the real light;
- brightness `1..100` controls the real light;
- physical changes report `device-originated`;
- commanded changes report `commanded`;
- a disconnect makes the UI/HA entity unavailable rather than falsely online;
- reconnect/power-cycle returns the device online without reconfiguration.

If a device never reaches the controller journal after saving setup, troubleshoot Wi-Fi/server configuration first. If it reaches the controller but is rejected, the structured `registration_rejected` event contains the reason.
