# Confirmed Varilight/SUPLA protocol subset

This document describes the wire behavior implemented by `protocol.py` and confirmed against a VARILIGHT V-Pro Smart dimmer reporting firmware **2.8.55** on 23 September 2026.

The 1 October source sync adds the replacement firmware 26.08 profile observed on 30 September: TLS 1.2, registration G, envelope version 25. Full physical stability is not established by registration.

It is intentionally a compatibility map, not a general SUPLA protocol implementation.

## Transport

The tested dimmer connects to TCP port **2016** using:

- TLS 1.1 only;
- cipher `AES128-SHA`;
- SUPLA protocol envelope version 12.

The server now permits TLS 1.1 through TLS 1.2 with `ECDHE-RSA-AES128-GCM-SHA256:AES128-SHA:@SECLEVEL=0`. The replacement negotiated TLS 1.2 with the ECDHE cipher. This is legacy cryptography required for device compatibility; keep the service on a trusted LAN.

## Frame envelope

SUPLA frames are packed without alignment padding:

| Field | Offset | Size |
|---|---:|---:|
| leading `SUPLA` tag | 0 | 5 |
| protocol version | 5 | 1 |
| `rr_id` | 6 | 4, little-endian |
| call ID | 10 | 4, little-endian |
| payload size | 14 | 4, little-endian |
| payload | 18 | variable |
| trailing `SUPLA` tag | 18 + payload | 5 |

The fixed header is 18 bytes; total framing overhead is 23 bytes. The Python header format is `<5sBIII`.

## Calls used by this project

| Direction | Call | ID |
|---|---|---:|
| device -> server | `PING_SERVER` | 40 |
| server -> device | `PING_SERVER_RESULT` | 50 |
| device -> server | `REGISTER_DEVICE_E` | 69 |
| server -> device | `REGISTER_DEVICE_RESULT` | 70 |
| device -> server | `REGISTER_DEVICE_G` | 76 |
| server -> device | `REGISTER_DEVICE_RESULT_B` | 71 |
| device -> server | channel value changed | 100 / 102 / 103 |
| server -> device | `CHANNEL_SET_VALUE` | 110 |
| device -> server | `CHANNEL_SET_VALUE_RESULT` | 120 |
| device -> server | `SET_ACTIVITY_TIMEOUT` | 210 |
| server -> device | `SET_ACTIVITY_TIMEOUT_RESULT` | 220 |

Unknown calls are logged rather than guessed. Malformed known calls are contained to the affected session.

## Registration

Firmware 2.8.55 registered with call 69, a **609-byte payload** representing the fixed registration data plus one channel.

A successful stock-compatible registration result uses result code **3** (`SUPLA_RESULTCODE_TRUE`). The response frame envelope remains protocol version 12 for this device, while the registration-result payload advertises server version 28/minimum 1.

### Replacement registration G

Only (registration E, version 12) and (registration G, version 25) pairs are accepted. G uses the shared 584-byte fixed header and channel-E format `<BiiiqBI8sBB` (36 bytes), giving 620 bytes for one channel. Flags are 64-bit; offline, validity, default icon and subdevice ID are preserved by decoding. Exact length and channel-count checks apply.

The controller still requires one channel, number 0, type 4000, brightness 0..100. G receives reply call 71: `<iBBBH` header (result, timeout, server version, minimum version, report length), followed by `01` for registered channel zero. The payload is ten bytes. E retains call 70 and its seven-byte payload. Unknown GUIDs retain the enrollment path. AuthKey is not validated; see SECURITY.md.

## Activity timeout and ping

Immediately after successful registration, the tested firmware sends call 210 requesting activity timeout `10` (`0x0A`). The accepted range used by the stock server is 10..240 seconds, so the response payload for call 220 is:

```text
0A 0A F0
selected=10, minimum=10, maximum=240
```

The dimmer then sends call-40 pings containing a **16-byte timeval** (two signed 64-bit values). A valid call-50 response also carries a 16-byte current timeval.

Without valid ping replies the firmware retries roughly every second. With valid replies it settles to approximately five-second keepalives.

The web UI deliberately filters these routine ping/ping-result events because they otherwise dominate the journal.

## Brightness/state reports

Calls 100, 102 and 103 carry variants of the channel-value-change payload. For this dimmer family, byte 0 of the 8-byte channel value is the literal brightness percentage `0..100`.

The controller treats a report that is not the completion of a pending command as device-originated state. This allows physical knob operation to propagate back to MQTT/Home Assistant.

## Brightness commands

Call 110 (`CHANNEL_SET_VALUE`) is a 17-byte payload containing sender ID, channel number, duration and an 8-byte value. This implementation writes the requested brightness literally into byte 0 and zeros bytes 1..7.

The device acknowledges with call 120. Command correlation is used for diagnostics/RTT and to distinguish commanded changes from unsolicited physical reports.

OFF is brightness 0. ON is implemented above the wire protocol by restoring the last known non-zero brightness.

## Request/response IDs

Server packets use the daemon's own non-zero monotonically increasing uint32 `rr_id` sequence per session. They do **not** simply echo the device's incoming `rr_id`.

This matches stock server behavior and was included in live validation.

## Compatibility boundary

Do not assume another firmware revision uses exactly this transport or sequence. Before adding support for an untested revision, capture at minimum:

1. TLS version/cipher;
2. registration call/version/payload shape;
3. timeout negotiation;
4. ping width/behavior;
5. one physical brightness report;
6. one server brightness command and acknowledgement.

Add firmware-specific behavior explicitly rather than widening the parser based on guesses.
