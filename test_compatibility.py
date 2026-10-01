"""Regression coverage for the deployed firmware-26.08 compatibility patch."""
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import controller
import protocol as p
from test_controller import DummyApp, FakeConn, device, registration_packet


def registration_g(count=1, number=0, kind=4000, brightness=42):
    fixed = bytearray(registration_packet().payload[:584])
    fixed[-1] = count
    # Pack independently of the implementation's Struct instance.
    channel = struct.pack('<BiiiqBI8sBB', number, kind, 17, 180,
                          1 << 40, 1, 123456, bytes([brightness]) + bytes(7), 2, 3)
    return p.Packet(25, 91, 76, bytes(fixed) + channel * count)


class CompatibilityTests(unittest.TestCase):
    def session(self):
        app = DummyApp(controller.RuntimeRegistry([device()]))
        conn = FakeConn()
        return app, conn, controller.DeviceSession(app, conn, ('10.0.0.203', 12345))

    def test_g_fields_and_64bit_flags(self):
        packet = registration_g()
        self.assertEqual(len(packet.payload), 620)
        reg = p.decode_registration_g(packet.payload)
        self.assertEqual(reg['guid'], bytes.fromhex(device().guid.replace('-', '')))
        self.assertEqual(reg['channels'][0], dict(number=0, type=4000, func_list=17,
            default=180, flags=1 << 40, offline=1, validity=123456,
            value=b'\x2a' + bytes(7), default_icon=2, subdevice_id=3))

    def test_g_rejects_malformed_lengths_and_excess_count(self):
        payload = registration_g().payload
        excess = bytearray(payload); excess[583] = 129
        for bad in (b'', payload[:583], payload[:-1], payload + b'\0', bytes(excess)):
            with self.subTest(length=len(bad)), self.assertRaises(p.ProtocolError):
                p.decode_registration_g(bad)

    def test_g_registration_reply_and_command_flow(self):
        app, conn, session = self.session()
        self.assertTrue(session._handle_packet(registration_g()))
        reply = p.Packet.decode(conn.sent[-1])
        self.assertEqual((reply.version, reply.call_id, reply.rr_id), (25, 71, 1))
        self.assertEqual(reply.payload, struct.pack('<iBBBH', 3, 120, 28, 1, 1) + b'\x01')
        self.assertTrue(session._handle_packet(p.Packet(25, 2, 210, b'\x0a')))
        self.assertTrue(session._handle_packet(p.Packet(25, 3, 40, struct.pack('<qq', 1, 2))))
        self.assertTrue(session.keepalive_ready)
        self.assertEqual(app.registry.request_brightness('livingroom', 67), (True, 'queued'))
        session._drain_commands()
        command = p.Packet.decode(conn.sent[-1])
        self.assertEqual((command.version, command.call_id), (25, 110))
        self.assertEqual(p.SET_VALUE.unpack(command.payload)[3], bytes([67]) + bytes(7))

    def test_e_registration_keeps_legacy_reply(self):
        _, conn, session = self.session()
        self.assertTrue(session._handle_packet(registration_packet()))
        reply = p.Packet.decode(conn.sent[-1])
        self.assertEqual((reply.version, reply.call_id), (12, 70))
        self.assertEqual(reply.payload, struct.pack('<iBBB', 3, 120, 28, 1))

    def test_registration_version_pairs_are_not_interchangeable(self):
        for call, version in ((69, 25), (76, 12), (76, 28)):
            _, conn, session = self.session()
            self.assertFalse(session._handle_packet(p.Packet(version, 1, call, registration_g().payload)))
            self.assertFalse(session.registered)
            self.assertEqual(conn.sent, [])

    def test_g_layout_and_brightness_guards(self):
        for kwargs in ({'count': 0}, {'count': 2}, {'number': 1}, {'kind': 1}, {'brightness': 101}):
            with self.subTest(kwargs=kwargs):
                _, conn, session = self.session()
                self.assertFalse(session._handle_packet(registration_g(**kwargs)))
                self.assertFalse(session.registered)
                self.assertEqual(conn.sent, [])

    def test_unknown_g_guid_reaches_enrollment_without_registration(self):
        app, conn, session = self.session()
        app.registry = controller.RuntimeRegistry([])
        app.note_unknown_device = Mock(return_value=True)
        self.assertFalse(session._handle_packet(registration_g()))
        app.note_unknown_device.assert_called_once()
        self.assertFalse(session.registered)
        self.assertEqual(conn.sent, [])

    def test_result_b_defaults_to_packet_version(self):
        packet = p.Packet.decode(p.build_register_result_b(25, 9, 3, 120))
        self.assertEqual(packet.payload, struct.pack('<iBBBH', 3, 120, 25, 1, 1) + b'\x01')

    def test_tls_handshakes_for_both_device_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp)/'cert.pem', Path(tmp)/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=test'],
                check=True, capture_output=True)
            server = controller.build_tls_context(cert, key)
            for version, cipher in ((ssl.TLSVersion.TLSv1_1, 'AES128-SHA'),
                                    (ssl.TLSVersion.TLSv1_2, 'ECDHE-RSA-AES128-GCM-SHA256')):
                with self.subTest(version=version):
                    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    client.check_hostname = False
                    client.verify_mode = ssl.CERT_NONE
                    client.minimum_version = client.maximum_version = version
                    client.set_ciphers(cipher + ':@SECLEVEL=0')
                    a, b = socket.socketpair()
                    a.settimeout(5); b.settimeout(5)
                    errors = []
                    def accept():
                        try:
                            with server.wrap_socket(a, server_side=True) as conn:
                                conn.sendall(b'x')
                        except Exception as exc:
                            errors.append(exc)
                    thread = threading.Thread(target=accept)
                    thread.start()
                    try:
                        with client.wrap_socket(b) as conn:
                            self.assertEqual(conn.recv(1), b'x')
                            self.assertEqual(conn.cipher()[0], cipher)
                            self.assertEqual(conn.version(), 'TLSv1.1' if version == ssl.TLSVersion.TLSv1_1 else 'TLSv1.2')
                    finally:
                        thread.join(6)
                        a.close(); b.close()
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
