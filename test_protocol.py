import unittest

import protocol as p


def fixed(text: str, size: int) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) >= size:
        raise ValueError("fixture too long")
    return raw + b"\0" * (size - len(raw))


class PacketTests(unittest.TestCase):
    def test_source_constants_and_layout_sizes(self):
        self.assertEqual(p.TAG, b"SUPLA")
        self.assertEqual(p.HEADER.size, 18)
        self.assertEqual(p.FRAME_OVERHEAD, 23)
        self.assertEqual(p.SUPLA_PROTO_VERSION, 28)
        self.assertEqual(p.SUPLA_PROTO_VERSION_MIN, 1)
        self.assertEqual(p.MAX_DATA_SIZE, 10240)
        self.assertEqual(p.CHANNEL_MAX_COUNT, 128)
        self.assertEqual(
            (
                p.CALL_GETVERSION, p.CALL_GETVERSION_RESULT,
                p.CALL_PING, p.CALL_PING_RESULT,
                p.CALL_REGISTER_E, p.CALL_REGISTER_RESULT,
                p.CALL_VALUE_CHANGED, p.CALL_VALUE_CHANGED_B,
                p.CALL_VALUE_CHANGED_C, p.CALL_SET_VALUE,
                p.CALL_SET_VALUE_RESULT, p.CALL_SET_ACTIVITY_TIMEOUT,
                p.CALL_SET_ACTIVITY_TIMEOUT_RESULT,
            ),
            (10, 20, 40, 50, 69, 70, 100, 102, 103, 110, 120, 210, 220),
        )
        self.assertEqual(
            (
                p.REG_E_FIXED.size, p.REG_CHANNEL_C.size,
                p.REGISTER_RESULT.size, p.VALUE_BASE.size, p.VALUE_B.size,
                p.VALUE_C.size, p.SET_VALUE.size, p.SET_VALUE_RESULT.size,
                p.ACTIVITY_TIMEOUT.size, p.ACTIVITY_TIMEOUT_RESULT.size,
            ),
            (584, 25, 7, 9, 10, 14, 17, 6, 1, 3),
        )

    def test_frame_round_trip(self):
        packet = p.Packet(12, 0x01020304, p.CALL_VALUE_CHANGED, b"abc")
        raw = packet.encode()
        self.assertEqual(len(raw), p.FRAME_OVERHEAD + 3)
        self.assertEqual(raw[:5], b"SUPLA")
        self.assertEqual(raw[5], 12)
        self.assertEqual(raw[6:10], b"\x04\x03\x02\x01")
        self.assertEqual(raw[10:14], b"\x64\x00\x00\x00")
        self.assertEqual(raw[14:18], b"\x03\x00\x00\x00")
        self.assertEqual(raw[-5:], b"SUPLA")
        self.assertEqual(p.Packet.decode(raw), packet)

    def test_reject_bad_leading_tag(self):
        raw = bytearray(p.Packet(12, 1, 40, b"").encode())
        raw[0] = ord("X")
        with self.assertRaises(p.ProtocolError):
            p.Packet.decode(bytes(raw))

    def test_reject_bad_trailing_tag(self):
        raw = bytearray(p.Packet(12, 1, 40, b"").encode())
        raw[-1] = ord("X")
        with self.assertRaises(p.ProtocolError):
            p.Packet.decode(bytes(raw))

    def test_reject_wrong_size(self):
        raw = p.Packet(12, 1, 40, b"1234").encode()
        with self.assertRaises(p.ProtocolError):
            p.Packet.decode(raw[:-1])

    def test_reject_oversized_payload(self):
        with self.assertRaises(p.ProtocolError):
            p.Packet(12, 1, 40, b"x" * (p.MAX_DATA_SIZE + 1)).encode()

    def test_reject_out_of_range_ids(self):
        with self.assertRaises(p.ProtocolError):
            p.Packet(12, -1, 40).encode()
        with self.assertRaises(p.ProtocolError):
            p.Packet(12, 1, 0x100000000).encode()


class RegistrationTests(unittest.TestCase):
    def test_decode_registration_e_one_channel(self):
        auth = bytes(range(16))
        guid = bytes(range(16, 32))
        value = bytes([42]) + b"\0" * 7
        fixed_part = p.REG_E_FIXED.pack(
            fixed("admin@supla.local", p.EMAIL_SIZE), auth, guid,
            fixed("VARILIGHT V-PRO SMART", p.NAME_SIZE),
            fixed("2.8.55", p.SOFTVER_SIZE), fixed("10.0.0.240", p.SERVER_NAME_SIZE),
            5, 123, 456, 1,
        )
        channel = p.REG_CHANNEL_C.pack(0, 55, 7, 1, 2, value)
        decoded = p.decode_registration_e(fixed_part + channel)
        self.assertEqual(decoded["email"], "admin@supla.local")
        self.assertEqual(decoded["guid"], guid)
        self.assertEqual(decoded["name"], "VARILIGHT V-PRO SMART")
        self.assertEqual(decoded["soft_ver"], "2.8.55")
        self.assertEqual(decoded["manufacturer_id"], 123)
        self.assertEqual(decoded["product_id"], 456)
        self.assertEqual(len(decoded["channels"]), 1)
        self.assertEqual(decoded["channels"][0]["number"], 0)
        self.assertEqual(decoded["channels"][0]["value"], value)

    def test_registration_size_must_match_count(self):
        fixed_part = p.REG_E_FIXED.pack(
            b"\0" * p.EMAIL_SIZE, b"\0" * p.AUTHKEY_SIZE,
            b"\0" * p.GUID_SIZE, b"\0" * p.NAME_SIZE,
            b"\0" * p.SOFTVER_SIZE, b"\0" * p.SERVER_NAME_SIZE,
            0, 0, 0, 1,
        )
        with self.assertRaises(p.ProtocolError):
            p.decode_registration_e(fixed_part)

    def test_registration_channel_count_cannot_exceed_source_maximum(self):
        fixed_part = p.REG_E_FIXED.pack(
            b"\0" * p.EMAIL_SIZE, b"\0" * p.AUTHKEY_SIZE,
            b"\0" * p.GUID_SIZE, b"\0" * p.NAME_SIZE,
            b"\0" * p.SOFTVER_SIZE, b"\0" * p.SERVER_NAME_SIZE,
            0, 0, 0, p.CHANNEL_MAX_COUNT + 1,
        )
        channels = b"\0" * ((p.CHANNEL_MAX_COUNT + 1) * p.REG_CHANNEL_C.size)
        with self.assertRaises(p.ProtocolError):
            p.decode_registration_e(fixed_part + channels)


class ValueTests(unittest.TestCase):
    def test_decode_value_change_variants(self):
        value = bytes([87]) + b"\0" * 7
        self.assertEqual(p.decode_value_change(100, p.VALUE_BASE.pack(0, value))["value"], value)
        self.assertEqual(p.decode_value_change(102, p.VALUE_B.pack(0, 0, value))["offline"], 0)
        c = p.decode_value_change(103, p.VALUE_C.pack(0, 0, 3600, value))
        self.assertEqual(c["validity"], 3600)
        self.assertEqual(p.brightness_from_value(c["value"]), 87)

    def test_build_literal_brightness(self):
        raw = p.build_brightness(12, 77, 54, channel=0, sender_id=0, duration_ms=0)
        packet = p.Packet.decode(raw)
        self.assertEqual(packet.call_id, p.CALL_SET_VALUE)
        sender, channel, duration, value = p.SET_VALUE.unpack(packet.payload)
        self.assertEqual((sender, channel, duration), (0, 0, 0))
        self.assertEqual(value, bytes([54]) + b"\0" * 7)

    def test_set_value_result_success_is_source_char(self):
        decoded = p.decode_set_value_result(p.SET_VALUE_RESULT.pack(0, 123, -1))
        self.assertEqual(decoded["success"], -1)

    def test_brightness_range(self):
        with self.assertRaises(p.ProtocolError):
            p.build_brightness(12, 1, 101)

    def test_set_value_result(self):
        decoded = p.decode_set_value_result(p.SET_VALUE_RESULT.pack(0, 123, 1))
        self.assertEqual(decoded, {"channel": 0, "sender_id": 123, "success": 1})

class ResponseTests(unittest.TestCase):
    def test_register_result(self):
        packet = p.Packet.decode(p.build_register_result(12, 9, 1, 60, 1))
        self.assertEqual(packet.call_id, p.CALL_REGISTER_RESULT)
        self.assertEqual(p.REGISTER_RESULT.unpack(packet.payload), (1, 60, 12, 1))

    def test_stock_server_registration_success_shape(self):
        packet = p.Packet.decode(
            p.build_register_result(12, 1, p.SUPLA_RESULTCODE_TRUE,
                                    p.DEFAULT_ACTIVITY_TIMEOUT, p.SUPLA_PROTO_VERSION_MIN,
                                    server_version=p.SUPLA_PROTO_VERSION)
        )
        self.assertEqual(packet.version, 12)
        self.assertEqual(packet.rr_id, 1)
        self.assertEqual(p.REGISTER_RESULT.unpack(packet.payload),
                         (3, 120, p.SUPLA_PROTO_VERSION, 1))

    def test_activity_timeout(self):
        self.assertEqual(p.decode_activity_timeout(p.ACTIVITY_TIMEOUT.pack(123)), 123)
        packet = p.Packet.decode(p.build_activity_timeout_result(12, 10, 5, 10, 20))
        self.assertEqual(packet.call_id, p.CALL_SET_ACTIVITY_TIMEOUT_RESULT)
        self.assertEqual(packet.payload, b"\x0a\x05\x14")
        self.assertEqual(p.ACTIVITY_TIMEOUT_RESULT.unpack(packet.payload), (10, 5, 20))

    def test_ping_result_timeval16(self):
        packet = p.Packet.decode(p.build_ping_result(12, 3, now_ns=1_700_000_000_123_456_789))
        self.assertEqual((packet.version, packet.rr_id, packet.call_id), (12, 3, p.CALL_PING_RESULT))
        self.assertEqual(len(packet.payload), 16)
        self.assertEqual(p.PING_TIMEVAL.unpack(packet.payload), (1_700_000_000, 123_456))


if __name__ == "__main__":
    unittest.main()
