from __future__ import annotations

from dataclasses import dataclass
import struct
import time

TAG = b"SUPLA"
HEADER = struct.Struct("<5sBIII")
FRAME_OVERHEAD = HEADER.size + len(TAG)

SUPLA_PROTO_VERSION = 28
SUPLA_PROTO_VERSION_MIN = 1
SUPLA_RESULTCODE_TRUE = 3
DEFAULT_ACTIVITY_TIMEOUT = 120
MAX_DATA_SIZE = 10240
CHANNEL_MAX_COUNT = 128

CALL_GETVERSION = 10
CALL_GETVERSION_RESULT = 20
CALL_PING = 40
CALL_PING_RESULT = 50
CALL_REGISTER_E = 69
CALL_REGISTER_RESULT = 70
CALL_VALUE_CHANGED = 100
CALL_VALUE_CHANGED_B = 102
CALL_VALUE_CHANGED_C = 103
CALL_SET_VALUE = 110
CALL_SET_VALUE_RESULT = 120
CALL_SET_ACTIVITY_TIMEOUT = 210
CALL_SET_ACTIVITY_TIMEOUT_RESULT = 220

EMAIL_SIZE = 256
AUTHKEY_SIZE = 16
GUID_SIZE = 16
NAME_SIZE = 201
SOFTVER_SIZE = 21
SERVER_NAME_SIZE = 65
VALUE_SIZE = 8
REG_E_FIXED = struct.Struct("<256s16s16s201s21s65sihhB")
REG_CHANNEL_C = struct.Struct("<Biiii8s")
REGISTER_RESULT = struct.Struct("<iBBB")
VALUE_BASE = struct.Struct("<B8s")
VALUE_B = struct.Struct("<BB8s")
VALUE_C = struct.Struct("<BBI8s")
SET_VALUE = struct.Struct("<iBI8s")
SET_VALUE_RESULT = struct.Struct("<Bib")
ACTIVITY_TIMEOUT = struct.Struct("<B")
ACTIVITY_TIMEOUT_RESULT = struct.Struct("<BBB")
PING_TIMEVAL = struct.Struct("<qq")

assert REG_E_FIXED.size == 584
assert REG_CHANNEL_C.size == 25
assert REGISTER_RESULT.size == 7
assert VALUE_BASE.size == 9
assert VALUE_B.size == 10
assert VALUE_C.size == 14
assert SET_VALUE.size == 17
assert SET_VALUE_RESULT.size == 6
assert ACTIVITY_TIMEOUT.size == 1
assert ACTIVITY_TIMEOUT_RESULT.size == 3
assert PING_TIMEVAL.size == 16


def _cstring(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    version: int
    rr_id: int
    call_id: int
    payload: bytes = b""

    def encode(self) -> bytes:
        if not 0 <= self.version <= 255:
            raise ProtocolError("version must fit uint8")
        if not 0 <= self.rr_id <= 0xFFFFFFFF:
            raise ProtocolError("rr_id must fit uint32")
        if not 0 <= self.call_id <= 0xFFFFFFFF:
            raise ProtocolError("call_id must fit uint32")
        if len(self.payload) > MAX_DATA_SIZE:
            raise ProtocolError("payload too large")
        return HEADER.pack(TAG, self.version, self.rr_id, self.call_id, len(self.payload)) + self.payload + TAG

    @classmethod
    def decode(cls, raw: bytes) -> "Packet":
        if len(raw) < FRAME_OVERHEAD:
            raise ProtocolError("frame too short")
        tag, version, rr_id, call_id, data_size = HEADER.unpack_from(raw)
        if tag != TAG:
            raise ProtocolError("bad leading tag")
        if data_size > MAX_DATA_SIZE:
            raise ProtocolError("payload too large")
        expected = FRAME_OVERHEAD + data_size
        if len(raw) != expected:
            raise ProtocolError(f"frame length {len(raw)} != expected {expected}")
        if raw[-len(TAG):] != TAG:
            raise ProtocolError("bad trailing tag")
        return cls(version, rr_id, call_id, raw[HEADER.size:-len(TAG)])


def decode_registration_e(payload: bytes) -> dict:
    if len(payload) < REG_E_FIXED.size:
        raise ProtocolError("registration-E payload too short")
    fixed = REG_E_FIXED.unpack_from(payload)
    channel_count = fixed[-1]
    if channel_count > CHANNEL_MAX_COUNT:
        raise ProtocolError(f"registration-E channel count {channel_count} exceeds {CHANNEL_MAX_COUNT}")
    expected = REG_E_FIXED.size + channel_count * REG_CHANNEL_C.size
    if len(payload) != expected:
        raise ProtocolError(f"registration-E size {len(payload)} != {expected}")
    channels = []
    offset = REG_E_FIXED.size
    for _ in range(channel_count):
        number, channel_type, func_list, default, flags, value = REG_CHANNEL_C.unpack_from(payload, offset)
        channels.append({"number": number, "type": channel_type, "func_list": func_list,
                         "default": default, "flags": flags, "value": value})
        offset += REG_CHANNEL_C.size
    return {
        "email": _cstring(fixed[0]), "auth_key": fixed[1], "guid": fixed[2],
        "name": _cstring(fixed[3]), "soft_ver": _cstring(fixed[4]),
        "server_name": _cstring(fixed[5]), "flags": fixed[6],
        "manufacturer_id": fixed[7], "product_id": fixed[8],
        "channels": channels,
    }


def decode_value_change(call_id: int, payload: bytes) -> dict:
    if call_id == CALL_VALUE_CHANGED:
        channel, value = VALUE_BASE.unpack(payload)
        return {"channel": channel, "offline": None, "validity": None, "value": value}
    if call_id == CALL_VALUE_CHANGED_B:
        channel, offline, value = VALUE_B.unpack(payload)
        return {"channel": channel, "offline": offline, "validity": None, "value": value}
    if call_id == CALL_VALUE_CHANGED_C:
        channel, offline, validity, value = VALUE_C.unpack(payload)
        return {"channel": channel, "offline": offline, "validity": validity, "value": value}
    raise ProtocolError(f"unsupported value-change call {call_id}")


def build_register_result(packet_version: int, rr_id: int, result_code: int,
                          activity_timeout: int, version_min: int = SUPLA_PROTO_VERSION_MIN,
                          server_version: int | None = None) -> bytes:
    if server_version is None:
        server_version = packet_version
    payload = REGISTER_RESULT.pack(result_code, activity_timeout, server_version, version_min)
    return Packet(packet_version, rr_id, CALL_REGISTER_RESULT, payload).encode()


def build_brightness(version: int, rr_id: int, brightness: int, channel: int = 0,
                     sender_id: int = 0, duration_ms: int = 0) -> bytes:
    if not 0 <= brightness <= 100:
        raise ProtocolError("brightness must be 0..100")
    value = bytes([brightness]) + b"\0" * 7
    payload = SET_VALUE.pack(sender_id, channel, duration_ms, value)
    return Packet(version, rr_id, CALL_SET_VALUE, payload).encode()


def decode_set_value_result(payload: bytes) -> dict:
    channel, sender_id, success = SET_VALUE_RESULT.unpack(payload)
    return {"channel": channel, "sender_id": sender_id, "success": success}


def decode_activity_timeout(payload: bytes) -> int:
    return ACTIVITY_TIMEOUT.unpack(payload)[0]


def build_activity_timeout_result(version: int, rr_id: int, minimum: int,
                                  selected: int, maximum: int) -> bytes:
    payload = ACTIVITY_TIMEOUT_RESULT.pack(selected, minimum, maximum)
    return Packet(version, rr_id, CALL_SET_ACTIVITY_TIMEOUT_RESULT, payload).encode()


def build_ping_result(version: int, rr_id: int, *, now_ns: int | None = None) -> bytes:
    if now_ns is None:
        now_ns = time.time_ns()
    sec, rem = divmod(now_ns, 1_000_000_000)
    usec = rem // 1_000
    payload = PING_TIMEVAL.pack(sec, usec)
    return Packet(version, rr_id, CALL_PING_RESULT, payload).encode()


def brightness_from_value(value: bytes) -> int:
    if len(value) != VALUE_SIZE:
        raise ProtocolError("channel value must be exactly 8 bytes")
    return value[0]


# Firmware 2.8.55 uses a 16-byte little-endian timeval (<qq) for ping replies.
