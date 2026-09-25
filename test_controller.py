import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import controller
import protocol as p

LIVING = "00112233-4455-6677-8899-AABBCCDDEEFF"
DINING = "10213243-5465-7687-98A9-BACBDCEDFE0F"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.queued = []
        self.closed = []

    def enqueue_brightness(self, value):
        self.queued.append(value)

    def close(self, reason="closed"):
        self.closed.append(reason)

class FakePublishResult:
    rc = 0

    def __init__(self, published=True, wait_error=None):
        self.published = published
        self.wait_error = wait_error
        self.wait_called = False

    def wait_for_publish(self, timeout=None):
        self.wait_called = True
        if self.wait_error is not None:
            raise self.wait_error

    def is_published(self):
        return self.published


class FakeMqttClient:
    def __init__(self):
        self.published = []
        self.subscriptions = []
        self.will = None
        self.credentials = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_subscribe = None
        self.on_message = None
        self.publish_result = FakePublishResult()
        self.disconnect_called = False
        self.disconnect_error = None
        self.loop_stop_called = False
        self.force_close_called = False
        self.subscribe_error = None
        self.subscribe_result = (0, 1)

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, **kwargs):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return self.publish_result

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        if self.subscribe_error is not None:
            raise self.subscribe_error
        return self.subscribe_result

    def connect_async(self, host, port, keepalive=30):
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        pass

    def disconnect(self):
        self.disconnect_called = True
        if self.disconnect_error is not None:
            raise self.disconnect_error

    def loop_stop(self):
        self.loop_stop_called = True

    def _sock_close(self):
        self.force_close_called = True


class TrackingRLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = threading.Event()

    def __enter__(self):
        self.attempted.set()
        self.lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.lock.release()


class BlockingPublishClient(FakeMqttClient):
    def __init__(self):
        super().__init__()
        self.block_topic = None
        self.block_once = True
        self.publish_entered = threading.Event()
        self.publish_release = threading.Event()

    def publish(self, topic, payload, qos=0, retain=False):
        if self.block_once and topic == self.block_topic:
            self.block_once = False
            self.publish_entered.set()
            self.publish_release.wait(timeout=2.0)
        return super().publish(topic, payload, qos=qos, retain=retain)


class UnconfirmedPublishResult:
    rc = 0

    def __init__(self):
        self.wait_called = False

    def wait_for_publish(self, timeout=None):
        self.wait_called = True


class BlockingSubscribeClient(FakeMqttClient):
    def __init__(self):
        super().__init__()
        self.subscribe_entered = threading.Event()
        self.subscribe_release = threading.Event()

    def subscribe(self, topic, qos=0):
        self.subscribe_entered.set()
        self.subscribe_release.wait(timeout=2.0)
        return super().subscribe(topic, qos)


class FakeSocket:
    def __init__(self):
        self.closed = False

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class SocketOnlyMqttClient(FakeMqttClient):
    def __init__(self):
        super().__init__()
        self._sock_close = None
        self.sock = FakeSocket()

    def socket(self):
        return self.sock


class NoAbortMqttClient(FakeMqttClient):
    def __init__(self):
        super().__init__()
        self._sock_close = None
        self.socket = None


class FakeConn:
    def __init__(self):
        self.sent = []
        self.timeout = None
        self.closed = False

    def sendall(self, raw):
        self.sent.append(raw)

    def version(self):
        return "TLSv1.1"

    def cipher(self):
        return ("AES128-SHA", "SSLv3", 128)

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class FakeNotifier:
    def __init__(self):
        self.snapshots = []

    def notify(self, snapshot):
        self.snapshots.append(snapshot)


class DummyApp:
    def __init__(self, registry):
        self.registry = registry
        self.mqtt = FakeNotifier()
        self.cfg = {"session_poll_seconds": 0.05}
        self.stop_event = threading.Event()

    def note_unknown_device(self, guid, peer, reg):
        return False


def device(name="livingroom", guid=LIVING):
    return controller.DeviceConfig(name, name.title(), guid)


def registration_packet(guid=LIVING, brightness=40, rr_id=1, channel_count=1,
                        channel_number=0, channel_type=4000):
    def fixed(text, size):
        raw = text.encode("utf-8")
        return raw + b"\0" * (size - len(raw))

    guid_bytes = bytes.fromhex(guid.replace("-", ""))
    fixed_part = p.REG_E_FIXED.pack(
        fixed("admin@supla.local", p.EMAIL_SIZE), b"\xAA" * p.AUTHKEY_SIZE,
        guid_bytes, fixed("VARILIGHT V-PRO SMART", p.NAME_SIZE),
        fixed("2.8.55", p.SOFTVER_SIZE), fixed("10.0.0.50", p.SERVER_NAME_SIZE),
        16, 7, 1, channel_count,
    )
    value = bytes([brightness]) + b"\0" * 7
    channels = b"".join(
        p.REG_CHANNEL_C.pack(channel_number + idx, channel_type, 0, 180, 65536, value)
        for idx in range(channel_count)
    )
    return p.Packet(12, rr_id, p.CALL_REGISTER_E, fixed_part + channels)


class RegistryTests(unittest.TestCase):
    def test_guid_normalisation_and_duplicate_rejection(self):
        self.assertEqual(controller.normalise_guid(LIVING.lower().replace("-", "")), LIVING)
        with self.assertRaises(ValueError):
            controller.normalise_guid("not-a-guid")
        with self.assertRaises(ValueError):
            controller.RuntimeRegistry([device(), device()])

    def test_on_restores_controller_owned_last_nonzero(self):
        clock = Clock(0.0)
        registry = controller.RuntimeRegistry([device()], clock=clock)
        session = FakeSession()
        registry.attach(LIVING, session, 0)
        ok, reason, target = registry.request_onoff("livingroom", "ON")
        self.assertEqual((ok, reason, target), (True, "queued", 100))
        self.assertEqual(registry.by_guid[LIVING].queued_target, 100)

        clock.value = 12.0
        registry.report_value(LIVING, session, 42)
        registry.request_onoff("livingroom", "OFF")
        self.assertEqual(registry.by_guid[LIVING].queued_target, 0)
        registry.request_onoff("livingroom", "ON")
        self.assertEqual(registry.by_guid[LIVING].queued_target, 42)

    def test_reconnect_replaces_session_without_old_detach_offlining_new(self):
        registry = controller.RuntimeRegistry([device()])
        old = FakeSession()
        new = FakeSession()
        self.assertIsNone(registry.attach(LIVING, old, 20))
        self.assertIs(registry.attach(LIVING, new, 20), old)
        stale_snapshot = registry.detach(LIVING, old, "replaced")
        state = registry.by_guid[LIVING]
        self.assertIsNone(stale_snapshot)
        self.assertTrue(state.connected)
        self.assertIs(state.session, new)
        self.assertEqual(state.reconnect_count, 1)

    def test_accepted_command_survives_session_replacement(self):
        registry = controller.RuntimeRegistry([device()])
        old = FakeSession()
        new = FakeSession()
        registry.attach(LIVING, old, 20)
        self.assertEqual(registry.request_brightness("livingroom", 66), (True, "queued"))
        self.assertEqual(registry.take_queued_command(LIVING, old), 66)
        registry.attach(LIVING, new, 20)
        self.assertEqual(registry.take_queued_command(LIVING, new), 66)

    def test_replaced_session_cannot_mutate_current_state(self):
        registry = controller.RuntimeRegistry([device()])
        old = FakeSession()
        new = FakeSession()
        registry.attach(LIVING, old, 20)
        registry.request_brightness("livingroom", 50)
        self.assertEqual(registry.take_queued_command(LIVING, old), 50)
        self.assertTrue(registry.note_command_sent(LIVING, old, 50))
        registry.attach(LIVING, new, 30)
        self.assertIsNone(registry.report_value(LIVING, old, 99))
        self.assertFalse(registry.note_command_sent(LIVING, old, 50))
        self.assertEqual(registry.by_guid[LIVING].brightness, 30)

    def test_origin_classification_and_rtt(self):
        clock = Clock(0.0)
        registry = controller.RuntimeRegistry([device()], clock=clock)
        session = FakeSession()
        registry.attach(LIVING, session, 10)
        self.assertEqual(registry.report_value(LIVING, session, 11)["origin"], "reconnect-startup")
        clock.value = 20.0
        self.assertEqual(registry.request_brightness("livingroom", 75), (True, "queued"))
        self.assertEqual(registry.take_queued_command(LIVING, session), 75)
        self.assertTrue(registry.note_command_sent(LIVING, session, 75))
        clock.value = 20.2
        snapshot = registry.report_value(LIVING, session, 75)
        self.assertEqual(snapshot["origin"], "commanded")
        self.assertAlmostEqual(snapshot["last_rtt_ms"], 200.0, places=3)
        clock.value = 30.0
        snapshot = registry.report_value(LIVING, session, 0)
        self.assertEqual(snapshot["origin"], "device-originated")
        self.assertEqual(snapshot["state_change_count"], 3)
        self.assertEqual(snapshot["device_originated_change_count"], 1)
        self.assertEqual(snapshot["device_originated_off_count"], 1)


class MqttTests(unittest.TestCase):
    def make_bridge(self):
        registry = controller.RuntimeRegistry([device()])
        client = FakeMqttClient()
        bridge = controller.MqttBridge(
            {"host": "10.0.0.10", "port": 1883, "username": "u"},
            registry,
            client_factory=lambda: client,
        )
        return registry, bridge, client

    def test_discovery_uses_guid_unique_id_and_dual_availability(self):
        registry, bridge, client = self.make_bridge()
        bridge.on_connect(client, None, None, 0)
        self.assertFalse(bridge.connected)
        self.assertEqual(bridge.pending_sub_mid, 1)
        bridge.on_subscribe(client, None, 1, [1])
        self.assertTrue(bridge.connected)
        self.assertIn(("mksi/lights/+/command/+", 1), client.subscriptions)
        self.assertEqual(client.will[:2], (controller.DAEMON_AVAILABILITY, "offline"))
        configs = [(t, json.loads(v)) for t, v, _, _ in client.published if t.startswith("homeassistant/light/")]
        self.assertEqual(len(configs), 1)
        topic, payload = configs[0]
        self.assertIn(LIVING.replace("-", "").lower(), topic)
        self.assertEqual(payload["availability_mode"], "all")
        self.assertEqual({x["topic"] for x in payload["availability"]}, {
            controller.DAEMON_AVAILABILITY,
            "mksi/lights/livingroom/availability",
        })
        self.assertNotIn("local/supla", json.dumps(payload))
        retained = {(topic, value) for topic, value, _, retain in client.published if retain}
        self.assertIn((controller.DAEMON_AVAILABILITY, "online"), retained)
        self.assertIn(("mksi/lights/livingroom/availability", "offline"), retained)

    def test_clear_device_removes_discovery_and_retained_runtime_topics(self):
        _, bridge, client = self.make_bridge()
        bridge.clear_device(device())
        cleared = {topic for topic, payload, _, retain in client.published if retain and payload == ""}
        uid = f"mksi_varilight_{LIVING.replace('-', '').lower()}"
        self.assertIn(f"homeassistant/light/{uid}/config", cleared)
        self.assertIn(f"homeassistant/sensor/{uid}_origin/config", cleared)
        self.assertIn(f"homeassistant/sensor/{uid}_last_change/config", cleared)
        self.assertIn("mksi/lights/livingroom/availability", cleared)
        self.assertIn("mksi/lights/livingroom/state/on", cleared)
        self.assertIn("mksi/lights/livingroom/state/brightness", cleared)

    def test_mqtt_command_enqueues_without_native_supla_topics(self):
        registry, bridge, client = self.make_bridge()
        session = FakeSession()
        registry.attach(LIVING, session, 55)
        msg = SimpleNamespace(topic="mksi/lights/livingroom/command/brightness", payload=b"73")
        bridge.on_message(client, None, msg)
        self.assertEqual(registry.by_guid[LIVING].queued_target, 73)
        self.assertTrue(all("local/supla" not in topic for topic, *_ in client.published))

    def test_stop_falls_back_to_lwt_when_offline_publish_not_confirmed(self):
        _, bridge, client = self.make_bridge()
        bridge.connected = True
        client.publish_result = FakePublishResult(published=False)
        bridge.stop()
        self.assertIn((controller.DAEMON_AVAILABILITY, "offline", 1, True), client.published)
        self.assertTrue(client.publish_result.wait_called)
        self.assertFalse(client.disconnect_called)
        self.assertTrue(client.force_close_called)
        self.assertTrue(client.loop_stop_called)

    def test_stop_always_stops_loop_when_disconnect_throws(self):
        _, bridge, client = self.make_bridge()
        bridge.connected = True
        client.publish_result = FakePublishResult(published=True)
        client.disconnect_error = RuntimeError("disconnect failed")
        bridge.stop()
        self.assertTrue(client.disconnect_called)
        self.assertTrue(client.force_close_called)
        self.assertTrue(client.loop_stop_called)


    def test_stale_snapshot_cannot_overwrite_new_retained_state(self):
        registry = controller.RuntimeRegistry([device()])
        client = BlockingPublishClient()
        bridge = controller.MqttBridge(
            {"host": "10.0.0.10", "port": 1883, "username": "u"},
            registry,
            client_factory=lambda: client,
        )
        old_session = FakeSession()
        new_session = FakeSession()
        registry.attach(LIVING, old_session, 0)
        old_snapshot = registry.snapshot_for_name("livingroom")
        old_snapshot["origin"] = "old"
        old_snapshot["last_change"] = "2026-09-23T18:00:01+00:00"
        bridge.connected = True
        client.block_topic = "mksi/lights/livingroom/availability"

        old_thread = threading.Thread(target=bridge.publish_state, args=(old_snapshot,))
        old_thread.start()
        self.assertTrue(client.publish_entered.wait(timeout=1.0))

        registry.attach(LIVING, new_session, 80)
        new_snapshot = registry.snapshot_for_name("livingroom")
        new_snapshot["origin"] = "new"
        new_snapshot["last_change"] = "2026-09-23T18:00:02+00:00"
        new_thread = threading.Thread(target=bridge.notify, args=(new_snapshot,))
        new_thread.start()
        self.assertTrue(new_thread.is_alive())

        client.publish_release.set()
        old_thread.join(timeout=2.0)
        new_thread.join(timeout=2.0)
        self.assertFalse(old_thread.is_alive())
        self.assertFalse(new_thread.is_alive())

        retained = {}
        for topic, payload, _, retain in client.published:
            if retain:
                retained[topic] = payload
        self.assertEqual(retained["mksi/lights/livingroom/availability"], "online")
        self.assertEqual(retained["mksi/lights/livingroom/state/brightness"], "80")
        self.assertEqual(retained["mksi/lights/livingroom/state/on"], "ON")
        self.assertEqual(retained["mksi/lights/livingroom/diagnostic/origin"], "new")
        self.assertEqual(retained["mksi/lights/livingroom/diagnostic/last_change"],
                         "2026-09-23T18:00:02+00:00")

    def test_unconfirmed_publish_uses_public_socket_abort(self):
        registry = controller.RuntimeRegistry([device()])
        client = SocketOnlyMqttClient()
        client.publish_result = UnconfirmedPublishResult()
        bridge = controller.MqttBridge(
            {"host": "10.0.0.10", "port": 1883, "username": "u"},
            registry,
            client_factory=lambda: client,
        )
        bridge.connected = True
        bridge.stop()
        self.assertTrue(client.publish_result.wait_called)
        self.assertFalse(client.disconnect_called)
        self.assertTrue(client.sock.closed)
        self.assertTrue(client.loop_stop_called)

    def test_client_without_abort_api_is_rejected(self):
        registry = controller.RuntimeRegistry([device()])
        client = NoAbortMqttClient()
        with self.assertRaisesRegex(RuntimeError, "transport-abort"):
            controller.MqttBridge(
                {"host": "10.0.0.10", "port": 1883, "username": "u"},
                registry,
                client_factory=lambda: client,
            )

    def test_subscribe_failure_is_contained_and_recovery_succeeds(self):
        _, bridge, client = self.make_bridge()
        client.subscribe_error = RuntimeError("subscribe exploded")
        bridge.on_connect(client, None, None, 0)
        self.assertFalse(bridge.connected)
        self.assertTrue(client.force_close_called)

        client.subscribe_error = None
        client.subscribe_result = (1, 1)
        client.force_close_called = False
        bridge.on_connect(client, None, None, 0)
        self.assertFalse(bridge.connected)
        self.assertTrue(client.force_close_called)

        client.subscribe_result = (0, 2)
        client.force_close_called = False
        bridge.on_connect(client, None, None, 0)
        self.assertFalse(bridge.connected)
        self.assertEqual(bridge.pending_sub_mid, 2)
        self.assertNotIn((controller.DAEMON_AVAILABILITY, "online", 1, True), client.published)
        bridge.on_subscribe(client, None, 2, [1])
        self.assertTrue(bridge.connected)
        self.assertFalse(client.force_close_called)
        self.assertIn((controller.DAEMON_AVAILABILITY, "online", 1, True), client.published)

    def test_suback_rejection_stale_mid_and_reconnect_recovery(self):
        _, bridge, client = self.make_bridge()
        bridge.on_connect(client, None, None, 0)
        self.assertEqual(bridge.pending_sub_mid, 1)
        bridge.on_subscribe(client, None, 1, [128])
        self.assertFalse(bridge.connected)
        self.assertTrue(client.force_close_called)
        self.assertFalse(any(topic == controller.DAEMON_AVAILABILITY and payload == "online"
                             for topic, payload, _, _ in client.published))

        client.force_close_called = False
        client.subscribe_result = (0, 2)
        bridge.on_connect(client, None, None, 0)
        self.assertEqual(bridge.pending_sub_mid, 2)
        bridge.on_subscribe(client, None, 1, [1])
        self.assertFalse(bridge.connected)
        self.assertTrue(client.force_close_called)

        client.force_close_called = False
        client.subscribe_result = (0, 3)
        bridge.on_connect(client, None, None, 0)
        bridge.on_subscribe(client, None, 3, [1])
        self.assertTrue(bridge.connected)
        online = [(topic, payload) for topic, payload, _, _ in client.published
                  if topic == controller.DAEMON_AVAILABILITY and payload == "online"]
        self.assertEqual(len(online), 1)

    def test_connect_shutdown_overlap_blocks_late_suback_and_late_connect(self):
        registry = controller.RuntimeRegistry([device()])
        client = BlockingSubscribeClient()
        bridge = controller.MqttBridge(
            {"host": "10.0.0.10", "port": 1883, "username": "u"},
            registry,
            client_factory=lambda: client,
        )
        tracking_lock = TrackingRLock()
        bridge.lifecycle_lock = tracking_lock
        connect_thread = threading.Thread(target=bridge.on_connect,
                                          args=(client, None, None, 0))
        connect_thread.start()
        self.assertTrue(client.subscribe_entered.wait(timeout=1.0))
        tracking_lock.attempted.clear()

        stop_thread = threading.Thread(target=bridge.stop)
        stop_thread.start()
        self.assertTrue(tracking_lock.attempted.wait(timeout=1.0))
        self.assertTrue(stop_thread.is_alive())

        client.subscribe_release.set()
        connect_thread.join(timeout=2.0)
        stop_thread.join(timeout=2.0)
        self.assertFalse(connect_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertTrue(bridge.stopped)
        self.assertFalse(bridge.connected)
        self.assertTrue(client.force_close_called)
        self.assertFalse(any(topic == controller.DAEMON_AVAILABILITY and payload == "online"
                             for topic, payload, _, _ in client.published))

        subscriptions_before = len(client.subscriptions)
        bridge.on_subscribe(client, None, 1, [1])
        bridge.on_connect(client, None, None, 0)
        self.assertEqual(len(client.subscriptions), subscriptions_before)
        self.assertFalse(bridge.connected)
        self.assertFalse(any(topic == controller.DAEMON_AVAILABILITY and payload == "online"
                             for topic, payload, _, _ in client.published))


class IdleConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.closed_event = threading.Event()

    def recv(self, size):
        self.closed_event.wait(timeout=5.0)
        return b""

    def shutdown(self, how):
        self.closed_event.set()

    def close(self):
        self.closed = True
        self.closed_event.set()


class BlockingTLSContext:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.tls_conn = FakeConn()

    def wrap_socket(self, raw, server_side=True, do_handshake_on_connect=False):
        self.entered.set()
        self.release.wait(timeout=2.0)
        return self.tls_conn


class BlockingHandshakeConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.handshake_entered = threading.Event()
        self.closed_event = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def do_handshake(self):
        self.handshake_entered.set()
        self.closed_event.wait(timeout=2.0)
        if self.closed:
            raise OSError("closed during handshake")

    def shutdown(self, how):
        self.closed_event.set()

    def close(self):
        self.closed = True
        self.closed_event.set()


class ImmediateTLSContext:
    def __init__(self):
        self.tls_conn = BlockingHandshakeConn()

    def wrap_socket(self, raw, server_side=True, do_handshake_on_connect=False):
        return self.tls_conn


class RegisteredIdleConn(IdleConn):
    def __init__(self, first_packet):
        super().__init__()
        self.first_packet = first_packet
        self.first_read = True
        self.registration_replied = threading.Event()

    def recv(self, size):
        if self.first_read:
            self.first_read = False
            return self.first_packet
        return super().recv(size)

    def sendall(self, raw):
        super().sendall(raw)
        self.registration_replied.set()


class ControllerShutdownTests(unittest.TestCase):
    def test_stop_closes_unregistered_connection_and_worker_exits(self):
        client = FakeMqttClient()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
            "session_poll_seconds": 0.05,
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client, tls_context=object())
        conn = IdleConn()
        session = controller.DeviceSession(app, conn, ("10.0.0.203", 12345))
        worker = threading.Thread(target=session.run)
        with app.connections_lock:
            app.connections.add(conn)
        with app.threads_lock:
            app.threads.add(worker)
        worker.start()
        app.stop()
        self.assertTrue(conn.closed)
        self.assertFalse(worker.is_alive())
        self.assertTrue(app.stop_event.is_set())
        self.assertTrue(client.loop_stop_called)


    def test_stop_wins_raw_to_tls_tracking_race(self):
        client = FakeMqttClient()
        tls_context = BlockingTLSContext()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
            "session_poll_seconds": 0.05,
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client,
                                       tls_context=tls_context)
        raw = IdleConn()
        self.assertTrue(app._track_connection(raw))
        self.assertTrue(app._start_session_thread(raw, ("10.0.0.203", 12345)))
        self.assertTrue(tls_context.entered.wait(timeout=1.0))

        stop_thread = threading.Thread(target=app.stop)
        stop_thread.start()
        self.assertTrue(app.stop_event.wait(timeout=1.0))
        self.assertTrue(raw.closed_event.wait(timeout=1.0))
        tls_context.release.set()
        stop_thread.join(timeout=2.0)

        self.assertFalse(stop_thread.is_alive())
        self.assertTrue(raw.closed)
        self.assertTrue(tls_context.tls_conn.closed)
        with app.threads_lock:
            self.assertFalse(any(thread.is_alive() for thread in app.threads))
        with app.connections_lock:
            self.assertEqual(app.connections, set())

    def test_stop_closes_tls_after_tracking_replacement(self):
        client = FakeMqttClient()
        tls_context = ImmediateTLSContext()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
            "session_poll_seconds": 0.05,
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client,
                                       tls_context=tls_context)
        raw = FakeConn()
        self.assertTrue(app._track_connection(raw))
        self.assertTrue(app._start_session_thread(raw, ("10.0.0.203", 12345)))
        self.assertTrue(tls_context.tls_conn.handshake_entered.wait(timeout=1.0))
        with app.connections_lock:
            self.assertNotIn(raw, app.connections)
            self.assertIn(tls_context.tls_conn, app.connections)

        stop_thread = threading.Thread(target=app.stop)
        stop_thread.start()
        self.assertTrue(tls_context.tls_conn.closed_event.wait(timeout=1.0))
        stop_thread.join(timeout=2.0)
        self.assertFalse(stop_thread.is_alive())
        self.assertTrue(tls_context.tls_conn.closed)
        with app.threads_lock:
            self.assertFalse(any(thread.is_alive() for thread in app.threads))
        with app.connections_lock:
            self.assertEqual(app.connections, set())

    def test_stop_closes_registered_worker_and_detaches_current_session(self):
        client = FakeMqttClient()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
            "session_poll_seconds": 0.05,
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client,
                                       tls_context=object())
        conn = RegisteredIdleConn(registration_packet(brightness=33).encode())
        session = controller.DeviceSession(app, conn, ("10.0.0.203", 12345))
        worker = threading.Thread(target=session.run)
        with app.connections_lock:
            app.connections.add(conn)
        with app.threads_lock:
            app.threads.add(worker)
        worker.start()
        self.assertTrue(conn.registration_replied.wait(timeout=1.0))
        with app.registry.lock:
            state = app.registry.by_guid[LIVING]
            self.assertIs(state.session, session)
            self.assertTrue(state.connected)

        app.stop()
        self.assertFalse(worker.is_alive())
        with app.registry.lock:
            state = app.registry.by_guid[LIVING]
            self.assertIsNone(state.session)
            self.assertFalse(state.connected)
        self.assertTrue(conn.closed)

    def test_new_connection_is_rejected_after_shutdown_begins(self):
        client = FakeMqttClient()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client,
                                       tls_context=object())
        app.stop_event.set()
        raw = FakeConn()
        self.assertFalse(app._track_connection(raw))
        self.assertFalse(app._start_session_thread(raw, ("10.0.0.203", 12345)))
        with app.connections_lock:
            self.assertNotIn(raw, app.connections)


class DeviceSessionTests(unittest.TestCase):
    def make_session(self):
        registry = controller.RuntimeRegistry([device()])
        app = DummyApp(registry)
        conn = FakeConn()
        session = controller.DeviceSession(app, conn, ("10.0.0.203", 12345))
        return registry, app, conn, session

    def test_known_registration_timeout_ping_and_literal_command(self):
        registry, app, conn, session = self.make_session()
        self.assertTrue(session._handle_packet(registration_packet(brightness=33)))
        reg_reply = p.Packet.decode(conn.sent[-1])
        self.assertEqual((reg_reply.call_id, reg_reply.rr_id), (p.CALL_REGISTER_RESULT, 1))
        self.assertEqual(p.REGISTER_RESULT.unpack(reg_reply.payload), (3, 120, 28, 1))

        timeout = p.Packet(12, 2, p.CALL_SET_ACTIVITY_TIMEOUT, p.ACTIVITY_TIMEOUT.pack(10))
        self.assertTrue(session._handle_packet(timeout))
        timeout_reply = p.Packet.decode(conn.sent[-1])
        self.assertEqual((timeout_reply.call_id, timeout_reply.rr_id), (p.CALL_SET_ACTIVITY_TIMEOUT_RESULT, 2))
        self.assertEqual(p.ACTIVITY_TIMEOUT_RESULT.unpack(timeout_reply.payload), (10, 10, 240))

        ping = p.Packet(12, 3, p.CALL_PING, p.PING_TIMEVAL.pack(1, 2))
        self.assertTrue(session._handle_packet(ping))
        self.assertEqual(p.Packet.decode(conn.sent[-1]).call_id, p.CALL_PING_RESULT)
        self.assertTrue(session.keepalive_ready)

        self.assertEqual(registry.request_brightness("livingroom", 87), (True, "queued"))
        session._drain_commands()
        command = p.Packet.decode(conn.sent[-1])
        self.assertEqual(command.call_id, p.CALL_SET_VALUE)
        self.assertEqual(p.brightness_from_value(p.SET_VALUE.unpack(command.payload)[3]), 87)
        self.assertEqual(command.rr_id, 4)

    def test_unknown_guid_is_rejected_without_response(self):
        registry, app, conn, session = self.make_session()
        unknown = "11111111-2222-3333-4444-555555555555"
        self.assertFalse(session._handle_packet(registration_packet(guid=unknown)))
        self.assertEqual(conn.sent, [])
        self.assertFalse(registry.by_name["livingroom"].connected)

    def test_call100_updates_state_and_notifies_mqtt(self):
        registry, app, conn, session = self.make_session()
        session._handle_packet(registration_packet(brightness=10))
        registry.by_guid[LIVING].connected_since = -100.0
        value = bytes([61]) + b"\0" * 7
        packet = p.Packet(12, 4, p.CALL_VALUE_CHANGED, p.VALUE_BASE.pack(0, value))
        self.assertTrue(session._handle_packet(packet))
        snapshot = registry.by_guid[LIVING].snapshot()
        self.assertEqual(snapshot["brightness"], 61)
        self.assertEqual(snapshot["origin"], "device-originated")
        self.assertEqual(app.mqtt.snapshots[-1]["brightness"], 61)

    def test_malformed_known_calls_are_contained(self):
        registry, _, _, session = self.make_session()
        session._handle_packet(registration_packet(brightness=10))
        self.assertTrue(session._handle_packet(p.Packet(12, 2, p.CALL_SET_ACTIVITY_TIMEOUT, b"")))
        self.assertTrue(session._handle_packet(p.Packet(12, 3, p.CALL_VALUE_CHANGED, b"\x00")))
        self.assertTrue(session._handle_packet(p.Packet(12, 4, p.CALL_SET_VALUE_RESULT, b"\x00")))
        value = bytes([44]) + b"\0" * 7
        valid = p.Packet(12, 5, p.CALL_VALUE_CHANGED, p.VALUE_BASE.pack(0, value))
        self.assertTrue(session._handle_packet(valid))
        self.assertEqual(registry.by_guid[LIVING].brightness, 44)

    def test_command_before_keepalive_survives_replacement_until_ready(self):
        registry, app, old_conn, old_session = self.make_session()
        self.assertTrue(old_session._handle_packet(registration_packet(brightness=25)))
        self.assertEqual(registry.request_brightness("livingroom", 66), (True, "queued"))
        old_count = len(old_conn.sent)
        old_session._drain_commands()
        self.assertEqual(len(old_conn.sent), old_count)

        new_conn = FakeConn()
        new_session = controller.DeviceSession(app, new_conn, ("10.0.0.203", 12346))
        self.assertTrue(new_session._handle_packet(registration_packet(brightness=25)))
        new_count = len(new_conn.sent)
        new_session._drain_commands()
        self.assertEqual(len(new_conn.sent), new_count)

        timeout = p.Packet(12, 2, p.CALL_SET_ACTIVITY_TIMEOUT, p.ACTIVITY_TIMEOUT.pack(10))
        ping = p.Packet(12, 3, p.CALL_PING, p.PING_TIMEVAL.pack(1, 2))
        self.assertTrue(new_session._handle_packet(timeout))
        self.assertTrue(new_session._handle_packet(ping))
        new_session._drain_commands()
        command = p.Packet.decode(new_conn.sent[-1])
        self.assertEqual(command.call_id, p.CALL_SET_VALUE)
        self.assertEqual(p.brightness_from_value(p.SET_VALUE.unpack(command.payload)[3]), 66)
        self.assertEqual(command.rr_id, 4)

    def test_registration_rejects_wrong_channel_layouts(self):
        cases = (
            {"channel_count": 0},
            {"channel_count": 2},
            {"channel_number": 1},
            {"channel_type": 1234},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                registry, _, conn, session = self.make_session()
                self.assertFalse(session._handle_packet(registration_packet(**kwargs)))
                self.assertEqual(conn.sent, [])
                self.assertFalse(registry.by_name["livingroom"].connected)

    def test_concurrent_sessions_share_controller_without_rr_or_state_crosstalk(self):
        client = FakeMqttClient()
        cfg = {
            "cert": "unused.crt",
            "key": "unused.key",
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [
                {"name": "livingroom", "display_name": "Living Room", "guid": LIVING},
                {"name": "dining", "display_name": "Dining", "guid": DINING},
            ],
        }
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client,
                                       tls_context=object())
        conn_a, conn_b = FakeConn(), FakeConn()
        a = controller.DeviceSession(app, conn_a, ("10.0.0.203", 10001))
        b = controller.DeviceSession(app, conn_b, ("10.0.0.204", 10002))
        reg_a = threading.Thread(target=a._handle_packet,
                                 args=(registration_packet(guid=LIVING, brightness=10),))
        reg_b = threading.Thread(target=b._handle_packet,
                                 args=(registration_packet(guid=DINING, brightness=20),))
        reg_a.start(); reg_b.start(); reg_a.join(); reg_b.join()
        self.assertEqual(p.Packet.decode(conn_a.sent[0]).rr_id, 1)
        self.assertEqual(p.Packet.decode(conn_b.sent[0]).rr_id, 1)

        def ready(session):
            session._handle_packet(p.Packet(12, 2, p.CALL_SET_ACTIVITY_TIMEOUT,
                                            p.ACTIVITY_TIMEOUT.pack(10)))
            session._handle_packet(p.Packet(12, 3, p.CALL_PING,
                                            p.PING_TIMEVAL.pack(1, 2)))

        ready_a = threading.Thread(target=ready, args=(a,))
        ready_b = threading.Thread(target=ready, args=(b,))
        ready_a.start(); ready_b.start(); ready_a.join(); ready_b.join()
        self.assertEqual([p.Packet.decode(raw).rr_id for raw in conn_a.sent[:3]], [1, 2, 3])
        self.assertEqual([p.Packet.decode(raw).rr_id for raw in conn_b.sent[:3]], [1, 2, 3])

        value_a = bytes([31]) + b"\0" * 7
        value_b = bytes([72]) + b"\0" * 7
        report_a = threading.Thread(target=a._handle_packet,
                                    args=(p.Packet(12, 4, p.CALL_VALUE_CHANGED,
                                                   p.VALUE_BASE.pack(0, value_a)),))
        report_b = threading.Thread(target=b._handle_packet,
                                    args=(p.Packet(12, 4, p.CALL_VALUE_CHANGED,
                                                   p.VALUE_BASE.pack(0, value_b)),))
        report_a.start(); report_b.start(); report_a.join(); report_b.join()
        self.assertEqual(app.registry.by_name["livingroom"].brightness, 31)
        self.assertEqual(app.registry.by_name["dining"].brightness, 72)

    def test_protocol_version_drift_closes_session(self):
        _, _, _, session = self.make_session()
        session._handle_packet(registration_packet(brightness=10))
        packet = p.Packet(11, 2, p.CALL_PING, p.PING_TIMEVAL.pack(1, 2))
        self.assertFalse(session._handle_packet(packet))

    def test_rr_id_sequence_is_per_session(self):
        _, _, conn_a, a = self.make_session()
        _, _, conn_b, b = self.make_session()
        a._handle_packet(registration_packet(rr_id=91))
        b._handle_packet(registration_packet(rr_id=92))
        self.assertEqual(p.Packet.decode(conn_a.sent[0]).rr_id, 1)
        self.assertEqual(p.Packet.decode(conn_b.sent[0]).rr_id, 1)


class ManagementTests(unittest.TestCase):
    def make_app(self, inventory_file):
        cfg = {
            "mqtt": {"host": "10.0.0.10", "port": 1883, "username": "u"},
            "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
            "cert": "x", "key": "y", "inventory_file": str(inventory_file),
        }
        client = FakeMqttClient()
        app = controller.ControllerApp(cfg, mqtt_client_factory=lambda: client, tls_context=object())
        app._schedule_restart = lambda: None
        return app, client

    def test_rename_persists_display_name_without_changing_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = Path(tmpdir) / "devices.json"
            app, _ = self.make_app(inventory)
            result = app.rename_device("livingroom", "Lounge")
            saved = json.loads(inventory.read_text(encoding="utf-8"))
            self.assertEqual(result["display_name"], "Lounge")
            self.assertEqual(saved[0]["name"], "livingroom")
            self.assertEqual(saved[0]["guid"], LIVING)
            self.assertEqual(saved[0]["display_name"], "Lounge")

    def test_delete_requires_mqtt_and_persists_removal_after_retained_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = Path(tmpdir) / "devices.json"
            app, client = self.make_app(inventory)
            with self.assertRaisesRegex(RuntimeError, "MQTT unavailable"):
                app.delete_device("livingroom", LIVING)
            app.mqtt.connected = True
            result = app.delete_device("livingroom", LIVING)
            self.assertEqual(result["guid"], LIVING)
            self.assertEqual(json.loads(inventory.read_text(encoding="utf-8")), [])
            self.assertTrue(any(topic.startswith("homeassistant/light/") and payload == ""
                                for topic, payload, _, retain in client.published if retain))

    def test_enrollment_captures_unknown_device_only_while_window_is_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = self.make_app(Path(tmpdir) / "devices.json")
            self.assertFalse(app.note_unknown_device(DINING, "10.0.0.201:1234", {"soft_ver": "2.8.55"}))
            app.start_enrollment(300)
            self.assertTrue(app.note_unknown_device(DINING, "10.0.0.201:1234", {"soft_ver": "2.8.55"}))
            status = app.enrollment_status()
            self.assertTrue(status["active"])
            self.assertEqual(status["candidates"][0]["guid"], DINING)
            self.assertEqual(status["candidates"][0]["firmware"], "2.8.55")

    def test_add_enrolled_candidate_persists_with_stable_internal_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = Path(tmpdir) / "devices.json"
            app, _ = self.make_app(inventory)
            app.start_enrollment(300)
            app.note_unknown_device(DINING, "10.0.0.201:1234", {"soft_ver": "2.8.55"})
            result = app.add_device(DINING, "Dining")
            saved = json.loads(inventory.read_text(encoding="utf-8"))
            self.assertEqual(result, {"name": "dining", "display_name": "Dining", "guid": DINING})
            self.assertEqual(saved[-1], result)
            self.assertFalse(app.enrollment_status()["active"])


class ConfigTests(unittest.TestCase):
    def test_inventory_file_overrides_static_devices_and_may_be_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            inventory = tmp / "devices.json"
            inventory.write_text("[]\n", encoding="utf-8")
            config = tmp / "config.json"
            config.write_text(json.dumps({
                "mqtt": {}, "cert": "x", "key": "y",
                "devices": [{"name": "livingroom", "display_name": "Living Room", "guid": LIVING}],
                "inventory_file": str(inventory),
            }), encoding="utf-8")
            loaded = controller.load_config(config)
            self.assertEqual(loaded["devices"], [])


if __name__ == "__main__":
    unittest.main()
