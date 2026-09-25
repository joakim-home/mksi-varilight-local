from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Callable, Iterable

try:
    import paho.mqtt.client as mqtt
except ImportError:  # Unit tests intentionally run without paho installed.
    mqtt = None

import protocol as p

LOG = logging.getLogger("mksi-varilightd")
OWN_ROOT = "mksi/lights"
DAEMON_AVAILABILITY = "mksi/varilightd/availability"
DISCOVERY_ROOT = "homeassistant"
VALUE_CALLS = {p.CALL_VALUE_CHANGED, p.CALL_VALUE_CHANGED_B, p.CALL_VALUE_CHANGED_C}
REGISTRATION_CALLS = {60, 65, 67, 68, 69, 75, 76}
ACTIVITY_TIMEOUT_MIN = 10
ACTIVITY_TIMEOUT_MAX = 240
COMMAND_WINDOW_SEC = 5.0
STARTUP_WINDOW_SEC = 10.0
DEFAULT_INVENTORY_FILE = "/var/lib/mksi-varilightd/devices.json"
DEFAULT_MANAGEMENT_BIND = "127.0.0.1"
DEFAULT_MANAGEMENT_PORT = 2017


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_guid(value: str | bytes) -> str:
    if isinstance(value, bytes):
        raw = value.hex().upper()
    else:
        raw = value.replace("-", "").strip().upper()
    if len(raw) != 32 or any(c not in "0123456789ABCDEF" for c in raw):
        raise ValueError(f"invalid GUID {value!r}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def log_event(kind: str, **fields: object) -> None:
    LOG.info(json.dumps({"event": kind, **fields}, separators=(",", ":"), default=str))


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    display_name: str
    guid: str


@dataclass
class DeviceState:
    config: DeviceConfig
    connected: bool = False
    brightness: int | None = None
    last_nonzero: int = 100
    origin: str | None = None
    last_change: str | None = None
    reconnect_count: int = 0
    state_change_count: int = 0
    device_originated_change_count: int = 0
    device_originated_off_count: int = 0
    last_disconnect_reason: str | None = None
    connected_since: float | None = None
    queued_target: int | None = None
    pending_target: int | None = None
    pending_sent: float | None = None
    last_rtt_ms: float | None = None
    revision: int = 0
    session: object | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        return {
            "name": self.config.name,
            "guid": self.config.guid,
            "connected": self.connected,
            "brightness": self.brightness,
            "last_nonzero": self.last_nonzero,
            "origin": self.origin,
            "last_change": self.last_change,
            "reconnect_count": self.reconnect_count,
            "state_change_count": self.state_change_count,
            "device_originated_change_count": self.device_originated_change_count,
            "device_originated_off_count": self.device_originated_off_count,
            "last_disconnect_reason": self.last_disconnect_reason,
            "last_rtt_ms": self.last_rtt_ms,
            "revision": self.revision,
        }


class FrameSplitter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> tuple[list[p.Packet], list[str]]:
        self.buffer.extend(data)
        packets: list[p.Packet] = []
        errors: list[str] = []
        while self.buffer:
            if len(self.buffer) < len(p.TAG):
                break
            if self.buffer[: len(p.TAG)] != p.TAG:
                idx = self.buffer.find(p.TAG, 1)
                if idx == -1:
                    keep = min(len(self.buffer), len(p.TAG) - 1)
                    discarded = len(self.buffer) - keep
                    if discarded:
                        errors.append(f"discarded {discarded} byte(s) before SUPLA tag")
                        del self.buffer[:discarded]
                    break
                errors.append(f"discarded {idx} byte(s) before SUPLA tag")
                del self.buffer[:idx]
            if len(self.buffer) < p.HEADER.size:
                break
            _, _, _, _, data_size = p.HEADER.unpack_from(self.buffer)
            if data_size > p.MAX_DATA_SIZE:
                errors.append(f"oversized payload length {data_size}")
                del self.buffer[0]
                continue
            total = p.FRAME_OVERHEAD + data_size
            if len(self.buffer) < total:
                break
            try:
                packet = p.Packet.decode(bytes(self.buffer[:total]))
            except p.ProtocolError as exc:
                errors.append(f"frame decode failed: {exc}")
                del self.buffer[0]
                continue
            packets.append(packet)
            del self.buffer[:total]
        return packets, errors


def build_tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    ctx.set_ciphers("AES128-SHA:@SECLEVEL=0")
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


class RuntimeRegistry:
    def __init__(self, devices: list[DeviceConfig], clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.by_guid = {item.guid: DeviceState(item) for item in devices}
        self.by_name = {item.name: self.by_guid[item.guid] for item in devices}
        if len(self.by_guid) != len(devices) or len(self.by_name) != len(devices):
            raise ValueError("device names and GUIDs must be unique")

    def known_guid(self, guid: str) -> bool:
        return normalise_guid(guid) in self.by_guid

    def state_for_name(self, name: str) -> DeviceState | None:
        return self.by_name.get(name)

    def snapshots(self) -> list[dict]:
        with self.lock:
            return [state.snapshot() for state in self.by_name.values()]

    def snapshot_for_name(self, name: str) -> dict | None:
        with self.lock:
            state = self.by_name.get(name)
            return None if state is None else state.snapshot()

    def snapshot_for_guid(self, guid: str) -> dict | None:
        with self.lock:
            state = self.by_guid.get(normalise_guid(guid))
            return None if state is None else state.snapshot()

    def device_configs(self) -> list[DeviceConfig]:
        with self.lock:
            return [state.config for state in self.by_name.values()]

    def attach(self, guid: str, session: object, initial_brightness: int | None) -> object | None:
        guid = normalise_guid(guid)
        with self.lock:
            state = self.by_guid[guid]
            old = state.session
            if state.connected_since is not None and old is not session:
                state.reconnect_count += 1
            if old is not None and old is not session and state.pending_target is not None and state.queued_target is None:
                state.queued_target = state.pending_target
            state.pending_target = None
            state.pending_sent = None
            state.session = session
            state.connected = True
            state.connected_since = self.clock()
            state.last_disconnect_reason = None
            if initial_brightness is not None:
                self._apply_value(state, initial_brightness, "reconnect-startup", None)
            state.revision += 1
            return old

    def detach(self, guid: str, session: object, reason: str) -> dict | None:
        guid = normalise_guid(guid)
        with self.lock:
            state = self.by_guid[guid]
            if state.session is not session:
                return None
            if state.pending_target is not None and state.queued_target is None:
                state.queued_target = state.pending_target
            state.session = None
            state.connected = False
            state.last_disconnect_reason = reason
            state.pending_target = None
            state.pending_sent = None
            state.revision += 1
            return state.snapshot()

    def take_queued_command(self, guid: str, session: object) -> int | None:
        with self.lock:
            state = self.by_guid[normalise_guid(guid)]
            if state.session is not session or not state.connected or state.queued_target is None:
                return None
            target = state.queued_target
            state.queued_target = None
            state.pending_target = target
            state.pending_sent = None
            return target

    def note_command_sent(self, guid: str, session: object, target: int) -> bool:
        with self.lock:
            state = self.by_guid[normalise_guid(guid)]
            if state.session is not session or state.pending_target != target:
                return False
            state.pending_sent = self.clock()
            return True

    def report_value(self, guid: str, session: object, brightness: int) -> dict | None:
        if not 0 <= brightness <= 100:
            raise ValueError("brightness must be 0..100")
        now = self.clock()
        with self.lock:
            state = self.by_guid[normalise_guid(guid)]
            if state.session is not session:
                return None
            rtt = None
            if (state.pending_target == brightness and state.pending_sent is not None and
                    now - state.pending_sent <= COMMAND_WINDOW_SEC):
                origin = "commanded"
                rtt = (now - state.pending_sent) * 1000.0
            elif state.connected_since is not None and now - state.connected_since <= STARTUP_WINDOW_SEC:
                origin = "reconnect-startup"
            else:
                origin = "device-originated"
            state.pending_target = None
            state.pending_sent = None
            changed = state.brightness != brightness
            if changed:
                state.state_change_count += 1
                if origin == "device-originated":
                    state.device_originated_change_count += 1
                    if brightness == 0:
                        state.device_originated_off_count += 1
            self._apply_value(state, brightness, origin, rtt)
            state.revision += 1
            return state.snapshot()

    def _apply_value(self, state: DeviceState, brightness: int, origin: str, rtt_ms: float | None) -> None:
        state.brightness = brightness
        if brightness > 0:
            state.last_nonzero = brightness
        state.origin = origin
        state.last_change = utc_now()
        if rtt_ms is not None:
            state.last_rtt_ms = rtt_ms

    def request_brightness(self, name: str, target: int) -> tuple[bool, str]:
        if not 0 <= target <= 100:
            return False, "brightness_out_of_range"
        with self.lock:
            state = self.by_name.get(name)
            if state is None:
                return False, "unknown_device"
            if not state.connected or state.session is None:
                return False, "device_offline"
            state.queued_target = target
            return True, "queued"

    def request_onoff(self, name: str, value: str) -> tuple[bool, str, int | None]:
        value = value.strip().upper()
        with self.lock:
            state = self.by_name.get(name)
            if state is None:
                return False, "unknown_device", None
            if value == "OFF":
                target = 0
            elif value == "ON":
                target = state.last_nonzero or 100
            else:
                return False, "invalid_onoff", None
        ok, reason = self.request_brightness(name, target)
        return ok, reason, target


class MqttBridge:
    def __init__(self, cfg: dict, registry: RuntimeRegistry, client_factory=None) -> None:
        self.cfg = cfg
        self.registry = registry
        self.connected = False
        self.transport_connected = False
        self.pending_sub_mid: int | None = None
        self.stopped = False
        self.lifecycle_lock = threading.RLock()
        self.publish_lock = threading.RLock()
        self.last_published_revision: dict[str, int] = {}
        if client_factory is not None:
            self.client = client_factory()
        else:
            if mqtt is None:
                raise RuntimeError("paho-mqtt is required for runtime use")
            try:
                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="mksi-varilightd")
            except (AttributeError, TypeError):
                self.client = mqtt.Client(client_id="mksi-varilightd")
        if not (callable(getattr(self.client, "_sock_close", None)) or callable(getattr(self.client, "socket", None))):
            raise RuntimeError("MQTT client lacks a transport-abort API required for LWT-safe shutdown")
        password = Path(cfg["password_file"]).read_text(encoding="utf-8").strip() if cfg.get("password_file") else None
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], password)
        self.client.will_set(DAEMON_AVAILABILITY, "offline", qos=1, retain=True)
        if hasattr(self.client, "reconnect_delay_set"):
            self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message

    @staticmethod
    def base(name: str) -> str:
        return f"{OWN_ROOT}/{name}"

    def publish(self, topic: str, payload: str, retain: bool = True) -> object | None:
        try:
            result = self.client.publish(topic, payload, qos=1, retain=retain)
            rc = getattr(result, "rc", 0)
            if rc not in (0, None):
                LOG.warning("MQTT publish rc=%s topic=%s", rc, topic)
                return None
            return result
        except Exception as exc:
            LOG.warning("MQTT publish failed topic=%s error=%s", topic, exc)
            return None

    def _abort_transport(self) -> bool:
        force_close = getattr(self.client, "_sock_close", None)
        if callable(force_close):
            try:
                force_close()
                return True
            except Exception as exc:
                LOG.warning("MQTT private socket close failed: %s", exc)
        socket_getter = getattr(self.client, "socket", None)
        if callable(socket_getter):
            try:
                sock = socket_getter()
            except Exception as exc:
                LOG.warning("MQTT socket lookup failed: %s", exc)
            else:
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        sock.close()
                        return True
                    except Exception as exc:
                        LOG.warning("MQTT socket close failed: %s", exc)
        return False

    @staticmethod
    def _mqtt_result_code(result) -> int:
        if isinstance(result, (tuple, list)) and result:
            return int(result[0])
        return int(getattr(result, "rc", result))

    @staticmethod
    def _suback_success(granted_qos) -> bool:
        if granted_qos is None:
            return False
        values = granted_qos if isinstance(granted_qos, (list, tuple)) else [granted_qos]
        if not values:
            return False
        for item in values:
            value = getattr(item, "value", item)
            try:
                code = int(value)
            except (TypeError, ValueError):
                return False
            if code >= 128:
                return False
        return True

    def on_connect(self, client, userdata, flags, rc, *extra) -> None:
        if int(rc) != 0:
            LOG.warning("MQTT connect failed rc=%s", rc)
            return
        with self.lifecycle_lock:
            if self.stopped:
                log_event("mqtt_connect_ignored", reason="stopping")
                self._abort_transport()
                return
            self.transport_connected = True
            self.connected = False
            self.pending_sub_mid = None
            try:
                subscribe_result = client.subscribe(f"{OWN_ROOT}/+/command/+", qos=1)
                subscribe_rc = self._mqtt_result_code(subscribe_result)
                if subscribe_rc != 0:
                    raise RuntimeError(f"subscribe rc={subscribe_rc}")
                if not isinstance(subscribe_result, (tuple, list)) or len(subscribe_result) < 2:
                    raise RuntimeError("subscribe result missing message id")
                self.pending_sub_mid = int(subscribe_result[1])
            except Exception as exc:
                self.connected = False
                self.transport_connected = False
                self.pending_sub_mid = None
                LOG.warning("MQTT subscribe failed; forcing reconnect: %s", exc)
                self._abort_transport()
                return
            log_event("mqtt_subscribe_pending", mid=self.pending_sub_mid)

    def on_subscribe(self, client, userdata, mid, granted_qos, *extra) -> None:
        with self.lifecycle_lock:
            if self.stopped:
                self.connected = False
                self.transport_connected = False
                self.pending_sub_mid = None
                log_event("mqtt_suback_ignored", mid=mid, reason="stopping")
                self._abort_transport()
                return
            expected = self.pending_sub_mid
            if expected is None or int(mid) != expected:
                self.connected = False
                self.transport_connected = False
                self.pending_sub_mid = None
                log_event("mqtt_suback_rejected", mid=mid, expected_mid=expected,
                          reason="stale_mid")
                self._abort_transport()
                return
            if not self._suback_success(granted_qos):
                self.connected = False
                self.transport_connected = False
                self.pending_sub_mid = None
                log_event("mqtt_suback_rejected", mid=mid, reason="broker_rejected",
                          granted_qos=granted_qos)
                self._abort_transport()
                return
            self.pending_sub_mid = None
            self.connected = True
            self.publish(DAEMON_AVAILABILITY, "online")
            for item in self.registry.device_configs():
                self.publish_discovery(item)
            for snapshot in self.registry.snapshots():
                self.publish_state(snapshot)
            log_event("mqtt_connected", host=self.cfg["host"],
                      port=int(self.cfg.get("port", 1883)), mid=mid)

    def on_disconnect(self, client, userdata, rc, *extra) -> None:
        with self.lifecycle_lock:
            self.connected = False
            self.transport_connected = False
            self.pending_sub_mid = None
        log_event("mqtt_disconnected", rc=rc)

    def on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            parts = msg.topic.split("/")
            if len(parts) != 5 or parts[:2] != ["mksi", "lights"] or parts[3] != "command":
                return
            name, command = parts[2], parts[4]
            if command == "on":
                ok, reason, target = self.registry.request_onoff(name, payload)
            elif command == "brightness":
                try:
                    target = int(float(payload))
                except (ValueError, OverflowError):
                    ok, reason, target = False, "invalid_brightness", None
                else:
                    ok, reason = self.registry.request_brightness(name, target)
            else:
                return
            log_event("mqtt_command", device=name, command=command, payload=payload,
                      target=target, accepted=ok, reason=reason)
        except Exception:
            LOG.exception("MQTT command handling failed topic=%s", msg.topic)

    def publish_discovery(self, item: DeviceConfig) -> None:
        base = self.base(item.name)
        guid_hex = item.guid.replace("-", "").lower()
        uid = f"mksi_varilight_{guid_hex}"
        device = {
            "identifiers": [uid],
            "name": item.display_name,
            "manufacturer": "VARILIGHT",
            "model": "V-Pro Smart (MKSI Local)",
        }
        availability = [
            {"topic": DAEMON_AVAILABILITY, "payload_available": "online", "payload_not_available": "offline"},
            {"topic": f"{base}/availability", "payload_available": "online", "payload_not_available": "offline"},
        ]
        light = {
            "name": None,
            "unique_id": uid,
            "state_topic": f"{base}/state/on",
            "command_topic": f"{base}/command/on",
            "payload_on": "ON",
            "payload_off": "OFF",
            "brightness_state_topic": f"{base}/state/brightness",
            "brightness_command_topic": f"{base}/command/brightness",
            "brightness_scale": 100,
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }
        self.publish(f"{DISCOVERY_ROOT}/light/{uid}/config", json.dumps(light, separators=(",", ":")))
        for key, label, device_class in (
            ("origin", "Last change origin", None),
            ("last_change", "Last change", "timestamp"),
        ):
            sensor = {
                "name": label,
                "unique_id": f"{uid}_{key}",
                "state_topic": f"{base}/diagnostic/{key}",
                "entity_category": "diagnostic",
                "availability": availability,
                "availability_mode": "all",
                "device": device,
            }
            if device_class:
                sensor["device_class"] = device_class
            self.publish(f"{DISCOVERY_ROOT}/sensor/{uid}_{key}/config",
                         json.dumps(sensor, separators=(",", ":")))

    def clear_device(self, item: DeviceConfig) -> None:
        base = self.base(item.name)
        guid_hex = item.guid.replace("-", "").lower()
        uid = f"mksi_varilight_{guid_hex}"
        topics = [
            f"{DISCOVERY_ROOT}/light/{uid}/config",
            f"{DISCOVERY_ROOT}/sensor/{uid}_origin/config",
            f"{DISCOVERY_ROOT}/sensor/{uid}_last_change/config",
            f"{base}/availability",
            f"{base}/state/on",
            f"{base}/state/brightness",
            f"{base}/diagnostic/origin",
            f"{base}/diagnostic/last_change",
        ]
        for topic in topics:
            info = self.publish(topic, "", retain=True)
            if info is None:
                raise RuntimeError(f"failed to clear retained topic {topic}")
            waiter = getattr(info, "wait_for_publish", None)
            if callable(waiter):
                waiter(timeout=2.0)
            checker = getattr(info, "is_published", None)
            if callable(checker) and not checker():
                raise RuntimeError(f"retained clear not confirmed for {topic}")
        self.last_published_revision.pop(item.name, None)
        log_event("mqtt_device_cleared", device=item.name, guid=item.guid)

    def publish_state(self, snapshot: dict) -> None:
        name = snapshot["name"]
        revision = int(snapshot.get("revision", -1))
        with self.publish_lock:
            current = self.registry.snapshot_for_name(name)
            if current is None or revision < int(current.get("revision", -1)):
                log_event("mqtt_stale_snapshot_dropped", device=name, revision=revision,
                          current_revision=None if current is None else current.get("revision"))
                return
            if revision < self.last_published_revision.get(name, -1):
                log_event("mqtt_stale_snapshot_dropped", device=name, revision=revision,
                          published_revision=self.last_published_revision[name])
                return
            base = self.base(name)
            self.publish(f"{base}/availability", "online" if snapshot["connected"] else "offline")
            brightness = snapshot["brightness"]
            if brightness is not None:
                self.publish(f"{base}/state/brightness", str(brightness))
                self.publish(f"{base}/state/on", "ON" if brightness > 0 else "OFF")
            if snapshot["origin"]:
                self.publish(f"{base}/diagnostic/origin", snapshot["origin"])
            if snapshot["last_change"]:
                self.publish(f"{base}/diagnostic/last_change", snapshot["last_change"])
            self.last_published_revision[name] = revision

    def notify(self, snapshot: dict | None) -> None:
        if snapshot is None:
            return
        with self.lifecycle_lock:
            active = self.connected and not self.stopped
        if active:
            self.publish_state(snapshot)

    def start(self) -> None:
        with self.lifecycle_lock:
            self.stopped = False
            self.connected = False
            self.transport_connected = False
            self.pending_sub_mid = None
        try:
            self.client.connect_async(self.cfg["host"], int(self.cfg.get("port", 1883)), keepalive=30)
            self.client.loop_start()
        except Exception as exc:
            LOG.warning("MQTT startup failed; device controller continues: %s", exc)

    def stop(self) -> None:
        with self.lifecycle_lock:
            if self.stopped:
                return
            self.stopped = True
            connected = self.connected
            transport_connected = self.transport_connected
            self.pending_sub_mid = None

        clean_disconnect = False
        if connected:
            confirmed = False
            info = self.publish(DAEMON_AVAILABILITY, "offline")
            if info is not None:
                waiter = getattr(info, "wait_for_publish", None)
                if callable(waiter):
                    try:
                        waiter(timeout=2.0)
                    except Exception as exc:
                        LOG.warning("MQTT offline publish wait failed: %s", exc)
                checker = getattr(info, "is_published", None)
                if callable(checker):
                    try:
                        confirmed = bool(checker())
                    except Exception as exc:
                        LOG.warning("MQTT offline publish confirmation failed: %s", exc)
            if confirmed:
                try:
                    self.client.disconnect()
                    clean_disconnect = True
                except Exception as exc:
                    LOG.warning("MQTT disconnect failed after offline publish: %s", exc)
            if not clean_disconnect:
                if not self._abort_transport():
                    LOG.error("MQTT transport abort unavailable; process exit must trigger LWT")
        elif transport_connected:
            if not self._abort_transport():
                LOG.error("MQTT pending-subscription transport abort unavailable")

        with self.lifecycle_lock:
            self.connected = False
            self.transport_connected = False
            self.pending_sub_mid = None

        try:
            self.client.loop_stop()
        except Exception as exc:
            LOG.warning("MQTT loop shutdown error: %s", exc)



class DeviceSession:
    def __init__(self, app: "ControllerApp", conn, peer: tuple[str, int]) -> None:
        self.app = app
        self.conn = conn
        self.peer = peer
        self.peer_text = f"{peer[0]}:{peer[1]}"
        self.guid: str | None = None
        self.version: int | None = None
        self.registered = False
        self.timeout_negotiated = False
        self.keepalive_ready = False
        self.next_rr_id = 1
        self.closed = threading.Event()

    def allocate_rr_id(self) -> int:
        result = self.next_rr_id
        self.next_rr_id = (result + 1) & 0xFFFFFFFF
        if self.next_rr_id == 0:
            self.next_rr_id = 1
        return result

    def close(self, reason: str = "closed") -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        log_event("session_close_requested", peer=self.peer_text, guid=self.guid, reason=reason)

    def _send(self, raw: bytes, kind: str, **fields: object) -> None:
        self.conn.sendall(raw)
        packet = p.Packet.decode(raw)
        log_event("supla_tx", peer=self.peer_text, guid=self.guid, call_id=packet.call_id,
                  rr_id=packet.rr_id, data_size=len(packet.payload), tx_kind=kind, **fields)

    def _drain_commands(self) -> None:
        if not self.keepalive_ready or self.guid is None or self.version is None:
            return
        target = self.app.registry.take_queued_command(self.guid, self)
        if target is None:
            return
        raw = p.build_brightness(self.version, self.allocate_rr_id(), target,
                                 channel=0, sender_id=0, duration_ms=0)
        self._send(raw, "set_value", brightness=target)
        if not self.app.registry.note_command_sent(self.guid, self, target):
            log_event("stale_command_send", peer=self.peer_text, guid=self.guid, brightness=target)

    def _handle_registration(self, packet: p.Packet) -> bool:
        if packet.version != 12:
            log_event("registration_rejected", peer=self.peer_text, reason="unsupported_protocol_version",
                      version=packet.version)
            return False
        try:
            reg = p.decode_registration_e(packet.payload)
            guid = normalise_guid(reg["guid"])
        except (p.ProtocolError, ValueError) as exc:
            log_event("registration_rejected", peer=self.peer_text, reason="decode_error", error=str(exc))
            return False
        if len(reg["channels"]) != 1:
            log_event("registration_rejected", peer=self.peer_text, reason="unexpected_channel_count",
                      guid=guid, channel_count=len(reg["channels"]))
            return False
        channel = reg["channels"][0]
        if channel["number"] != 0 or channel["type"] != 4000:
            log_event("registration_rejected", peer=self.peer_text, reason="unexpected_channel_layout",
                      guid=guid, channel=channel["number"], channel_type=channel["type"])
            return False
        initial = p.brightness_from_value(channel["value"])
        if not 0 <= initial <= 100:
            log_event("registration_rejected", peer=self.peer_text, reason="invalid_initial_brightness",
                      guid=guid, brightness=initial)
            return False
        if not self.app.registry.known_guid(guid):
            pending = self.app.note_unknown_device(guid, self.peer_text, reg)
            log_event("registration_rejected", peer=self.peer_text,
                      reason="pending_enrollment" if pending else "unknown_guid", guid=guid)
            return False
        self.guid = guid
        self.version = packet.version
        old = self.app.registry.attach(guid, self, initial)
        if old is not None and old is not self:
            old.close("replaced_by_reconnect")
        self.registered = True
        raw = p.build_register_result(packet.version, self.allocate_rr_id(), p.SUPLA_RESULTCODE_TRUE,
                                      p.DEFAULT_ACTIVITY_TIMEOUT, p.SUPLA_PROTO_VERSION_MIN,
                                      p.SUPLA_PROTO_VERSION)
        self._send(raw, "register_result", result_code=p.SUPLA_RESULTCODE_TRUE)
        snapshot = self.app.registry.snapshot_for_guid(guid)
        self.app.mqtt.notify(snapshot)
        log_event("device_registered", peer=self.peer_text, guid=guid, firmware=reg["soft_ver"],
                  initial_brightness=initial, tls_version=self.conn.version(), cipher=self.conn.cipher())
        return True

    def _handle_packet(self, packet: p.Packet) -> bool:
        if not self.registered:
            if packet.call_id != p.CALL_REGISTER_E:
                log_event("pre_registration_call_ignored", peer=self.peer_text, call_id=packet.call_id,
                          data_size=len(packet.payload))
                return True
            return self._handle_registration(packet)

        if packet.version != self.version:
            log_event("protocol_version_mismatch", peer=self.peer_text, guid=self.guid,
                      expected=self.version, received=packet.version, call_id=packet.call_id)
            return False

        if packet.call_id == p.CALL_SET_ACTIVITY_TIMEOUT:
            try:
                requested = p.decode_activity_timeout(packet.payload)
            except (p.ProtocolError, ValueError, struct.error) as exc:
                log_event("known_call_decode_error", peer=self.peer_text, guid=self.guid,
                          call_id=packet.call_id, error=str(exc))
                return True
            selected = max(ACTIVITY_TIMEOUT_MIN, min(ACTIVITY_TIMEOUT_MAX, requested))
            raw = p.build_activity_timeout_result(packet.version, self.allocate_rr_id(),
                                                  ACTIVITY_TIMEOUT_MIN, selected, ACTIVITY_TIMEOUT_MAX)
            self._send(raw, "set_activity_timeout_result", selected=selected,
                       minimum=ACTIVITY_TIMEOUT_MIN, maximum=ACTIVITY_TIMEOUT_MAX)
            self.timeout_negotiated = True
            return True

        if packet.call_id == p.CALL_PING:
            if self.timeout_negotiated and len(packet.payload) == p.PING_TIMEVAL.size:
                raw = p.build_ping_result(packet.version, self.allocate_rr_id())
                self._send(raw, "ping_result")
                self.keepalive_ready = True
            else:
                log_event("ping_ignored", peer=self.peer_text, guid=self.guid,
                          payload_size=len(packet.payload), timeout_negotiated=self.timeout_negotiated)
            return True

        if packet.call_id in VALUE_CALLS:
            try:
                value = p.decode_value_change(packet.call_id, packet.payload)
                brightness = p.brightness_from_value(value["value"])
            except (p.ProtocolError, ValueError, struct.error) as exc:
                log_event("known_call_decode_error", peer=self.peer_text, guid=self.guid,
                          call_id=packet.call_id, error=str(exc))
                return True
            if value["channel"] != 0:
                log_event("channel_ignored", peer=self.peer_text, guid=self.guid,
                          channel=value["channel"], call_id=packet.call_id)
                return True
            snapshot = self.app.registry.report_value(self.guid, self, brightness)
            if snapshot is None:
                log_event("stale_session_state_ignored", peer=self.peer_text, guid=self.guid,
                          brightness=brightness)
                return True
            self.app.mqtt.notify(snapshot)
            log_event("device_state", peer=self.peer_text, guid=self.guid,
                      brightness=brightness, origin=snapshot["origin"],
                      command_rtt_ms=snapshot["last_rtt_ms"],
                      state_change_count=snapshot["state_change_count"],
                      device_originated_change_count=snapshot["device_originated_change_count"],
                      device_originated_off_count=snapshot["device_originated_off_count"])
            return True

        if packet.call_id == p.CALL_SET_VALUE_RESULT:
            try:
                result = p.decode_set_value_result(packet.payload)
            except (p.ProtocolError, ValueError, struct.error) as exc:
                log_event("known_call_decode_error", peer=self.peer_text, guid=self.guid,
                          call_id=packet.call_id, error=str(exc))
            else:
                log_event("set_value_result", peer=self.peer_text, guid=self.guid, **result)
            return True

        log_event("unknown_call", peer=self.peer_text, guid=self.guid,
                  call_id=packet.call_id, data_size=len(packet.payload))
        return True

    def run(self) -> None:
        splitter = FrameSplitter()
        reason = "peer_closed"
        log_event("tls_connected", peer=self.peer_text,
                  tls_version=self.conn.version(), cipher=self.conn.cipher())
        self.conn.settimeout(float(self.app.cfg.get("session_poll_seconds", 0.5)))
        try:
            while not self.closed.is_set() and not self.app.stop_event.is_set():
                self._drain_commands()
                try:
                    data = self.conn.recv(4096)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError) as exc:
                    reason = f"connection_error:{type(exc).__name__}"
                    break
                if not data:
                    break
                packets, errors = splitter.feed(data)
                for error in errors:
                    log_event("stream_warning", peer=self.peer_text, guid=self.guid, message=error)
                for packet in packets:
                    fields = {
                        "peer": self.peer_text, "guid": self.guid,
                        "version": packet.version, "rr_id": packet.rr_id,
                        "call_id": packet.call_id, "data_size": len(packet.payload),
                    }
                    if packet.call_id in REGISTRATION_CALLS:
                        fields["payload_hex"] = "<redacted registration payload>"
                    elif self.app.cfg.get("unsafe_raw_payload", False):
                        fields["payload_hex"] = packet.payload.hex().upper()
                    log_event("supla_rx", **fields)
                    if not self._handle_packet(packet):
                        reason = "registration_rejected"
                        return
                    self._drain_commands()
        finally:
            if self.guid is not None:
                snapshot = self.app.registry.detach(self.guid, self, reason)
                if snapshot is not None:
                    self.app.mqtt.notify(snapshot)
            try:
                self.conn.close()
            except Exception:
                pass
            log_event("tls_disconnected", peer=self.peer_text, guid=self.guid, reason=reason)


class ManagementHandler(BaseHTTPRequestHandler):
    server_version = "MKSI-Varilight-Management/1.0"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("management_http %s - %s", self.address_string(), fmt % args)

    def json_response(self, data, status=200) -> None:
        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def route(self) -> list[str]:
        return [unquote(part) for part in urlparse(self.path).path.strip("/").split("/") if part]

    def do_GET(self) -> None:
        route = self.route()
        if route == ["devices"]:
            return self.json_response({"devices": self.server.app.inventory()})
        if route == ["enrollment"]:
            return self.json_response(self.server.app.enrollment_status())
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parts = self.route()
        if parts == ["enrollment", "start"]:
            try:
                return self.json_response(self.server.app.start_enrollment(self.read_json().get("seconds", 300)))
            except (TypeError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parts == ["enrollment", "cancel"]:
            return self.json_response(self.server.app.cancel_enrollment())
        if parts == ["enrollment", "accept"]:
            try:
                body = self.read_json()
                device = self.server.app.add_device(body.get("guid"), body.get("display_name"))
                return self.json_response({"ok": True, "device": device, "restarting": True})
            except (TypeError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.CONFLICT)
            except Exception as exc:
                LOG.exception("enrollment accept failed")
                return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        if len(parts) == 3 and parts[0] == "devices" and parts[2] == "rename":
            try:
                result = self.server.app.rename_device(parts[1], self.read_json().get("display_name"))
                return self.json_response({"ok": True, "device": result, "restarting": True})
            except KeyError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (TypeError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                LOG.exception("rename failed")
                return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parts = self.route()
        if len(parts) == 2 and parts[0] == "devices":
            try:
                result = self.server.app.delete_device(parts[1], self.read_json().get("guid"))
                return self.json_response({"ok": True, "device": result, "restarting": True})
            except KeyError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (TypeError, ValueError) as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except Exception as exc:
                LOG.exception("delete failed")
                return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)


class ControllerApp:
    def __init__(self, cfg: dict, mqtt_client_factory=None,
                 tls_context: ssl.SSLContext | None = None) -> None:
        self.cfg = cfg
        devices = [
            DeviceConfig(
                name=item["name"],
                display_name=item.get("display_name", item["name"]),
                guid=normalise_guid(item["guid"]),
            )
            for item in cfg["devices"]
        ]
        self.registry = RuntimeRegistry(devices)
        self.mqtt = MqttBridge(cfg["mqtt"], self.registry, client_factory=mqtt_client_factory)
        self.tls_context = tls_context
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.threads: set[threading.Thread] = set()
        self.threads_lock = threading.Lock()
        self.connections: set[object] = set()
        self.connections_lock = threading.Lock()
        self.inventory_file = Path(cfg.get("inventory_file", DEFAULT_INVENTORY_FILE))
        self.management_bind = str(cfg.get("management_bind", DEFAULT_MANAGEMENT_BIND))
        self.management_port = int(cfg.get("management_port", DEFAULT_MANAGEMENT_PORT))
        self.management_server: ThreadingHTTPServer | None = None
        self.management_thread: threading.Thread | None = None
        self.restart_requested = threading.Event()
        self.enrollment_lock = threading.RLock()
        self.enrollment_until = 0.0
        self.enrollment_candidates: dict[str, dict] = {}

    def inventory(self) -> list[dict]:
        return [
            {"name": item.name, "display_name": item.display_name, "guid": item.guid}
            for item in self.registry.device_configs()
        ]

    def enrollment_status(self) -> dict:
        with self.enrollment_lock:
            remaining = max(0, int(self.enrollment_until - time.monotonic()))
            active = remaining > 0
            if not active:
                self.enrollment_candidates.clear()
            candidates = sorted(self.enrollment_candidates.values(), key=lambda x: x["first_seen"]) if active else []
            return {"active": active, "remaining_seconds": remaining, "candidates": candidates}

    def start_enrollment(self, seconds: int = 300) -> dict:
        seconds = max(30, min(600, int(seconds)))
        with self.enrollment_lock:
            self.enrollment_until = time.monotonic() + seconds
            self.enrollment_candidates.clear()
        log_event("enrollment_started", seconds=seconds)
        return self.enrollment_status()

    def cancel_enrollment(self) -> dict:
        with self.enrollment_lock:
            self.enrollment_until = 0.0
            self.enrollment_candidates.clear()
        log_event("enrollment_cancelled")
        return self.enrollment_status()

    def _persist_inventory(self, devices: list[DeviceConfig]) -> None:
        payload = [
            {"name": item.name, "display_name": item.display_name, "guid": item.guid}
            for item in devices
        ]
        self.inventory_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.inventory_file.with_name(self.inventory_file.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.inventory_file)

    @staticmethod
    def _validate_display_name(value) -> str:
        if not isinstance(value, str):
            raise TypeError("display_name must be a string")
        value = value.strip()
        if not value:
            raise ValueError("display_name cannot be empty")
        if len(value) > 64:
            raise ValueError("display_name must be 64 characters or fewer")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("display_name contains control characters")
        return value

    def note_unknown_device(self, guid: str, peer: str, reg: dict) -> bool:
        guid = normalise_guid(guid)
        with self.enrollment_lock:
            if time.monotonic() >= self.enrollment_until:
                return False
            now = utc_now()
            candidate = self.enrollment_candidates.get(guid)
            if candidate is None:
                candidate = {
                    "guid": guid, "peer": peer, "firmware": str(reg.get("soft_ver", "")),
                    "first_seen": now, "last_seen": now,
                }
                self.enrollment_candidates[guid] = candidate
                log_event("enrollment_candidate_detected", guid=guid, peer=peer, firmware=candidate["firmware"])
            else:
                candidate["peer"] = peer
                candidate["firmware"] = str(reg.get("soft_ver", candidate.get("firmware", "")))
                candidate["last_seen"] = now
            return True

    @staticmethod
    def _device_key(display_name: str, guid: str, existing: set[str]) -> str:
        base = "".join(ch.lower() for ch in display_name if ch.isalnum()) or "light"
        if base not in existing:
            return base
        suffix = guid.replace("-", "")[:8].lower()
        return f"{base}-{suffix}"

    def add_device(self, guid, display_name) -> dict:
        guid = normalise_guid(guid)
        display_name = self._validate_display_name(display_name)
        with self.enrollment_lock:
            if time.monotonic() >= self.enrollment_until:
                raise RuntimeError("enrollment window is not active")
            if guid not in self.enrollment_candidates:
                raise ValueError("GUID has not been detected during this enrollment window")
        devices = self.registry.device_configs()
        if any(item.guid == guid for item in devices):
            raise ValueError("device is already enrolled")
        name = self._device_key(display_name, guid, {item.name for item in devices})
        added = DeviceConfig(name, display_name, guid)
        self._persist_inventory(devices + [added])
        with self.enrollment_lock:
            self.enrollment_candidates.pop(guid, None)
            self.enrollment_until = 0.0
        log_event("device_add_requested", device=name, guid=guid, display_name=display_name)
        self._schedule_restart()
        return {"name": name, "display_name": display_name, "guid": guid}

    def rename_device(self, name: str, display_name) -> dict:
        display_name = self._validate_display_name(display_name)
        devices = self.registry.device_configs()
        current = next((item for item in devices if item.name == name), None)
        if current is None:
            raise KeyError(f"unknown device {name}")
        updated = [
            DeviceConfig(item.name, display_name if item.name == name else item.display_name, item.guid)
            for item in devices
        ]
        self._persist_inventory(updated)
        log_event("device_rename_requested", device=name, guid=current.guid,
                  old_display_name=current.display_name, new_display_name=display_name)
        self._schedule_restart()
        return {"name": name, "display_name": display_name, "guid": current.guid}

    def delete_device(self, name: str, confirm_guid) -> dict:
        devices = self.registry.device_configs()
        current = next((item for item in devices if item.name == name), None)
        if current is None:
            raise KeyError(f"unknown device {name}")
        if not isinstance(confirm_guid, str) or normalise_guid(confirm_guid) != current.guid:
            raise ValueError("GUID confirmation does not match device")
        if not self.mqtt.connected:
            raise RuntimeError("MQTT unavailable; refusing delete so retained discovery cannot become stale")
        snapshot = self.registry.snapshot_for_name(name)
        self.mqtt.clear_device(current)
        try:
            self._persist_inventory([item for item in devices if item.name != name])
        except Exception:
            self.mqtt.publish_discovery(current)
            if snapshot is not None:
                self.mqtt.publish_state(snapshot)
            raise
        log_event("device_delete_requested", device=name, guid=current.guid)
        self._schedule_restart()
        return {"name": current.name, "display_name": current.display_name, "guid": current.guid}

    def _schedule_restart(self) -> None:
        if self.restart_requested.is_set():
            return
        self.restart_requested.set()
        timer = threading.Timer(0.35, self.stop)
        timer.daemon = True
        timer.start()

    def _start_management(self) -> None:
        if self.management_server is not None:
            return
        server = ThreadingHTTPServer((self.management_bind, self.management_port), ManagementHandler)
        server.app = self
        thread = threading.Thread(target=server.serve_forever, name="mksi-management", daemon=True)
        self.management_server = server
        self.management_thread = thread
        thread.start()
        log_event("management_listening", bind=self.management_bind, port=self.management_port)

    def _track_connection(self, conn: object) -> bool:
        with self.connections_lock:
            if self.stop_event.is_set():
                return False
            self.connections.add(conn)
            return True

    def _replace_connection(self, old: object, new: object) -> bool:
        with self.connections_lock:
            self.connections.discard(old)
            if self.stop_event.is_set():
                return False
            self.connections.add(new)
            return True

    def _start_session_thread(self, raw: socket.socket, peer: tuple[str, int]) -> bool:
        thread = threading.Thread(target=self._session_worker, args=(raw, peer), daemon=True)
        with self.threads_lock:
            if self.stop_event.is_set():
                return False
            self.threads.add(thread)
            thread.start()
            return True

    def _context(self) -> ssl.SSLContext:
        if self.tls_context is None:
            self.tls_context = build_tls_context(Path(self.cfg["cert"]), Path(self.cfg["key"]))
        return self.tls_context

    def _session_worker(self, raw: socket.socket, peer: tuple[str, int]) -> None:
        thread = threading.current_thread()
        tracked = raw
        try:
            raw.settimeout(float(self.cfg.get("tls_handshake_timeout", 10.0)))
            tls_conn = self._context().wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            tracked = tls_conn
            if not self._replace_connection(raw, tls_conn):
                try:
                    tls_conn.close()
                except Exception:
                    pass
                return
            with tls_conn:
                tls_conn.do_handshake()
                DeviceSession(self, tls_conn, peer).run()
        except ssl.SSLError as exc:
            log_event("tls_error", peer=f"{peer[0]}:{peer[1]}", error=str(exc))
        except (ConnectionError, OSError) as exc:
            if not self.stop_event.is_set():
                log_event("connection_error", peer=f"{peer[0]}:{peer[1]}", error=str(exc))
        except Exception:
            LOG.exception("session failure from %s:%s", peer[0], peer[1])
        finally:
            with self.connections_lock:
                self.connections.discard(raw)
                self.connections.discard(tracked)
            try:
                tracked.close()
            except Exception:
                pass
            with self.threads_lock:
                self.threads.discard(thread)

    def serve_forever(self) -> None:
        self._start_management()
        self.mqtt.start()
        bind = self.cfg.get("bind", "10.0.0.50")
        port = int(self.cfg.get("port", 2016))
        backlog = int(self.cfg.get("listen_backlog", 8))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((bind, port))
            listener.listen(backlog)
            listener.settimeout(1.0)
            log_event("listening", bind=bind, port=port, mode="controller", devices=len(self.registry.by_name))
            while not self.stop_event.is_set():
                try:
                    raw, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                if not self._track_connection(raw):
                    try:
                        raw.close()
                    except Exception:
                        pass
                    break
                if not self._start_session_thread(raw, peer):
                    with self.connections_lock:
                        self.connections.discard(raw)
                    try:
                        raw.close()
                    except Exception:
                        pass
                    break
        self.listener = None
        self.mqtt.stop()

    def stop(self) -> None:
        self.stop_event.set()
        management = self.management_server
        self.management_server = None
        if management is not None:
            try:
                management.shutdown()
            except Exception:
                pass
            try:
                management.server_close()
            except Exception:
                pass
        listener = self.listener
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass

        for state in list(self.registry.by_name.values()):
            session = state.session
            if session is not None:
                session.close("daemon_stopping")

        deadline = time.monotonic() + 3.5
        while True:
            with self.connections_lock:
                connections = list(self.connections)
            for conn in connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

            with self.threads_lock:
                threads = [thread for thread in self.threads if thread.is_alive()]
            if not threads:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.warning("shutdown deadline reached with %d session worker(s) still alive", len(threads))
                break
            for thread in threads:
                thread.join(timeout=min(0.1, max(0.0, remaining)))

        self.mqtt.stop()


def load_config(path: str | Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"mqtt", "devices", "cert", "key"}
    missing = sorted(required - cfg.keys())
    if missing:
        raise ValueError(f"missing config keys: {', '.join(missing)}")
    inventory_file = Path(cfg.get("inventory_file", DEFAULT_INVENTORY_FILE))
    if inventory_file.is_file():
        inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
        if not isinstance(inventory, list):
            raise ValueError("inventory_file must contain a JSON list")
        cfg["devices"] = inventory
    return cfg


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MKSI Varilight local controller")
    parser.add_argument("--config", default="/etc/mksi-varilightd/config.json")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    cfg = load_config(args.config)
    app = ControllerApp(cfg)
    LOG.warning("Legacy TLS listener enabled for Varilight firmware compatibility; LAN only")
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        log_event("stopped", reason="keyboard_interrupt")
    finally:
        app.stop()


if __name__ == "__main__":
    main()
