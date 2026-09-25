#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

LOG = logging.getLogger("mksi-varilight-ui")
ROOT = "mksi/lights"
DAEMON_AVAILABILITY = "mksi/varilightd/availability"
WWW = Path(__file__).with_name("www")


def load_config(path: str | Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    password_file = Path(cfg["mqtt"]["password_file"])
    cfg["mqtt"]["password"] = password_file.read_text(encoding="utf-8-sig").strip()
    return cfg


class MqttState:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()
        self.connected = False
        self.daemon = "unknown"
        self.lights = {
            item["name"]: {"label": item.get("display_name", item["name"]), "guid": item["guid"]}
            for item in cfg["devices"]
        }
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="mksi-varilight-ui")
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id="mksi-varilight-ui")
        self.client.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, rc, *extra) -> None:
        with self.lock:
            self.connected = int(rc) == 0
        if int(rc) != 0:
            LOG.warning("MQTT connect failed rc=%s", rc)
            return
        for topic in (
            DAEMON_AVAILABILITY,
            f"{ROOT}/+/state/+",
            f"{ROOT}/+/availability",
            f"{ROOT}/+/diagnostic/+",
        ):
            client.subscribe(topic, qos=1)
        LOG.info("MQTT connected")

    def on_disconnect(self, client, userdata, rc, *extra) -> None:
        with self.lock:
            self.connected = False
        LOG.warning("MQTT disconnected rc=%s", rc)

    def on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        with self.lock:
            if msg.topic == DAEMON_AVAILABILITY:
                self.daemon = payload
                return
            parts = msg.topic.split("/")
            if len(parts) < 4 or parts[:2] != ["mksi", "lights"]:
                return
            name = parts[2]
            if name not in self.lights:
                return
            key = "/".join(parts[3:])
            self.lights[name][key] = payload

    def sync_devices(self, devices: list[dict]) -> None:
        with self.lock:
            current = self.lights
            updated = {}
            for item in devices:
                name = item["name"]
                state = dict(current.get(name, {}))
                state["label"] = item.get("display_name", name)
                state["guid"] = item["guid"]
                updated[name] = state
            self.lights = updated

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mqtt_connected": self.connected,
                "daemon": self.daemon,
                "lights": json.loads(json.dumps(self.lights)),
            }

    def command(self, name: str, field: str, value: str) -> None:
        if name not in self.lights:
            raise ValueError("unknown light")
        info = self.client.publish(f"{ROOT}/{name}/command/{field}", value, qos=1, retain=False)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            raise RuntimeError(f"MQTT publish failed rc={rc}")

    def start(self) -> None:
        self.client.connect_async(
            self.cfg["mqtt"]["host"],
            int(self.cfg["mqtt"].get("port", 1883)),
            keepalive=30,
        )
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()


class ControllerManagement:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=2.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise RuntimeError(detail) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"controller management unavailable: {exc}") from exc

    def devices(self) -> list[dict]:
        return self.request("/devices").get("devices", [])

    def rename(self, name: str, display_name: str) -> dict:
        return self.request(f"/devices/{quote(name, safe='')}/rename", "POST",
                            {"display_name": display_name})

    def delete(self, name: str, guid: str) -> dict:
        return self.request(f"/devices/{quote(name, safe='')}", "DELETE", {"guid": guid})

    def enrollment(self) -> dict:
        return self.request("/enrollment")

    def start_enrollment(self, seconds: int = 300) -> dict:
        return self.request("/enrollment/start", "POST", {"seconds": seconds})

    def cancel_enrollment(self) -> dict:
        return self.request("/enrollment/cancel", "POST", {})

    def accept_enrollment(self, guid: str, display_name: str) -> dict:
        return self.request("/enrollment/accept", "POST", {"guid": guid, "display_name": display_name})


class Handler(BaseHTTPRequestHandler):
    server_version = "MKSI-Varilight-UI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)

    def json_response(self, data, status=200) -> None:
        payload = json.dumps(data, separators=(",", ":")).encode()
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

    def sync_inventory(self) -> None:
        try:
            self.server.mqtt_state.sync_devices(self.server.management.devices())
        except Exception as exc:
            LOG.debug("inventory sync skipped: %s", exc)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self.sync_inventory()
            return self.json_response(self.server.mqtt_state.snapshot())
        if path == "/api/health":
            self.sync_inventory()
            snap = self.server.mqtt_state.snapshot()
            status = 200 if snap["mqtt_connected"] else 503
            return self.json_response({"ok": status == 200, **snap}, status)
        if path == "/api/enrollment":
            try:
                return self.json_response(self.server.management.enrollment())
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
        if path == "/api/logs":
            return self.serve_logs()
        self.serve_static(path)

    def serve_logs(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        source = query.get("source", ["controller"])[0]
        try:
            lines = max(20, min(250, int(query.get("lines", ["100"])[0])))
        except ValueError:
            lines = 100
        units = {
            "controller": ["mksi-varilightd.service"],
            "ui": ["mksi-varilight-ui.service"],
            "both": ["mksi-varilightd.service", "mksi-varilight-ui.service"],
        }
        if source not in units:
            return self.json_response({"error": "invalid log source"}, HTTPStatus.BAD_REQUEST)
        cmd = ["/usr/bin/journalctl", "--no-pager", "--quiet", "--output=short-iso", "--lines", str(lines)]
        for unit in units[source]: cmd.extend(["--unit", unit])
        try:
            result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self.json_response({"error": f"journal read failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        if result.returncode:
            return self.json_response({"error": result.stderr.strip() or "journalctl failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json_response({"source": source, "lines": lines, "text": result.stdout})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        parts = [part for part in path.strip("/").split("/") if part]
        if parts == ["api", "enrollment", "start"]:
            try:
                return self.json_response(self.server.management.start_enrollment(self.read_json().get("seconds", 300)))
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parts == ["api", "enrollment", "cancel"]:
            try:
                return self.json_response(self.server.management.cancel_enrollment())
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parts == ["api", "enrollment", "accept"]:
            try:
                body = self.read_json()
                return self.json_response(self.server.management.accept_enrollment(body.get("guid"), body.get("display_name")))
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if len(parts) == 4 and parts[:2] == ["api", "light"] and parts[3] == "rename":
            try:
                body = self.read_json()
                result = self.server.management.rename(parts[2], body.get("display_name"))
                return self.json_response(result)
            except Exception as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if not path.startswith("/api/light/"):
            return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            name = path.rsplit("/", 1)[-1]
            body = self.read_json()
            if "on" in body:
                self.server.mqtt_state.command(name, "on", "ON" if bool(body["on"]) else "OFF")
            if "brightness" in body:
                value = max(0, min(100, int(body["brightness"])))
                self.server.mqtt_state.command(name, "brightness", str(value))
            return self.json_response({"ok": True})
        except (ValueError, TypeError) as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("command failed path=%s", path)
            return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        parts = [part for part in path.strip("/").split("/") if part]
        if len(parts) != 3 or parts[:2] != ["api", "light"]:
            return self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            body = self.read_json()
            result = self.server.management.delete(parts[2], body.get("guid"))
            return self.json_response(result)
        except Exception as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (WWW / rel).resolve()
        root = WWW.resolve()
        if target != root and root not in target.parents:
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not target.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/mksi-varilight-ui/config.json")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    mqtt_state = MqttState(cfg)
    mqtt_state.start()
    management = ControllerManagement(cfg.get("controller_management_url", "http://127.0.0.1:2017"))
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.mqtt_state = mqtt_state
    server.management = management

    def stop_signal(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    LOG.info("MKSI Varilight UI listening on http://%s:%d", args.listen, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        mqtt_state.stop()


if __name__ == "__main__":
    main()
