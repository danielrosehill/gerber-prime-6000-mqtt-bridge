#!/usr/bin/env python3
"""
Gerber Prime 6000 <-> MQTT bridge, with Home Assistant MQTT discovery.

Bridges the controller's undocumented local HTTP API (POST /sdk.cgi, HTTP Basic auth,
double-URI-encoded JSON payloads) to MQTT. Home Assistant discovers the device
automatically: one switch entity per relay channel and one positional cover entity per
roller-shutter channel.

Device API reference: https://github.com/danielrosehill/gerber-prime-6000-local-api

Configuration is via environment variables (see .env.example / docker-compose.example.yml):

    GERBER_HOST     controller IP (default 10.10.10.254 = vendor SoftAP default)
    GERBER_USER     HTTP Basic user       (default admin)
    GERBER_PASS     HTTP Basic password   (default 888888 — vendor default)
    GERBER_UID      device uid from get_all_device (default 257)
    RELAY_CHIDS     comma-separated relay channel ids     (default "1,2,3,4,5,6")
    SHUTTER_CHIDS   comma-separated shutter channel ids   (default "7")
    MQTT_HOST / MQTT_PORT / MQTT_USER / MQTT_PASS
    MQTT_BASE_TOPIC state/command topic base   (default gerber/gps6000)
    DISCOVERY_PREFIX HA discovery prefix       (default homeassistant)
    DEVICE_NAME     HA device name             (default "Gerber Prime 6000")
    POLL_INTERVAL   seconds between state polls (default 10)

Semantics (reverse-engineered from the device web UI):
  - relays: `switch` command, val 0 = off, 255 = on
  - shutters: positional, val 0-99 (0 = closed); the controller does the time-based
    positioning itself using its configured travel time. There is NO stop command.
"""
import base64
import json
import logging
import os
import re
import signal
import threading
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

log = logging.getLogger("gerber-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GERBER_HOST = os.environ.get("GERBER_HOST", "10.10.10.254")
GERBER_USER = os.environ.get("GERBER_USER", "admin")
GERBER_PASS = os.environ.get("GERBER_PASS", "888888")
UID = int(os.environ.get("GERBER_UID", "257"))
RELAY_CHIDS = [int(x) for x in os.environ.get("RELAY_CHIDS", "1,2,3,4,5,6").split(",") if x.strip()]
SHUTTER_CHIDS = [int(x) for x in os.environ.get("SHUTTER_CHIDS", "7").split(",") if x.strip()]
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
BASE = os.environ.get("MQTT_BASE_TOPIC", "gerber/gps6000").strip("/")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").strip("/")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Gerber Prime 6000")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))

AVAIL_TOPIC = f"{BASE}/availability"
NODE_ID = re.sub(r"[^a-zA-Z0-9_-]", "_", BASE)
DEVICE = {
    "identifiers": [NODE_ID],
    "name": DEVICE_NAME,
    "manufacturer": "Gerber Prime",
    "model": "6000 series (PAN27/PSC05)",
    "configuration_url": f"http://{GERBER_HOST}/",
}


# ---- Gerber HTTP API -------------------------------------------------------
# The web UI double-encodes the payload: encodeURIComponent(encodeURI(json)).
# Responses are URI-encoded JSON; respcode 100 == OK.

def _encode_uri(s):
    return urllib.parse.quote(s, safe="!#$&'()*+,-./:;=?@_~")


def _encode_uri_component(s):
    return urllib.parse.quote(s, safe="!'()*-._~")


_AUTH = "Basic " + base64.b64encode(f"{GERBER_USER}:{GERBER_PASS}".encode()).decode()


def sdk(cmd, **fields):
    control = {"cmd": cmd}
    control.update(fields)
    payload = json.dumps({"control": control}, separators=(",", ":"))
    body = "json=" + _encode_uri_component(_encode_uri(payload))
    req = urllib.request.Request(
        f"http://{GERBER_HOST}/sdk.cgi",
        data=body.encode(),
        headers={"Authorization": _AUTH, "Content-Type": "application/x-www-form-urlencoded"},
    )
    raw = urllib.request.urlopen(req, timeout=10).read().decode(errors="replace")
    resp = json.loads(urllib.parse.unquote(raw))
    if resp.get("control", {}).get("respcode") != 100:
        raise RuntimeError(f"sdk {cmd} failed: {resp.get('control')}")
    return resp


def get_channels():
    """Return {chid: basicvalue}."""
    resp = sdk("get_all_device")
    return {c["chid"]: c["basicvalue"] for c in resp["device"][0]["channel"]}


def switch(chid, val):
    sdk("switch", uid=UID, chid=chid, val=val)


# ---- MQTT + HA discovery ---------------------------------------------------

def publish_discovery(client):
    for chid in RELAY_CHIDS:
        cfg = {
            "name": f"Relay {chid}",
            "unique_id": f"{NODE_ID}_relay_{chid}",
            "state_topic": f"{BASE}/relay/{chid}/state",
            "command_topic": f"{BASE}/relay/{chid}/set",
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICE,
        }
        client.publish(
            f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/relay_{chid}/config",
            json.dumps(cfg), retain=True,
        )
    for chid in SHUTTER_CHIDS:
        cfg = {
            "name": f"Roller Shutter {chid}" if len(SHUTTER_CHIDS) > 1 else "Roller Shutter",
            "unique_id": f"{NODE_ID}_shutter_{chid}",
            "device_class": "shutter",
            "command_topic": f"{BASE}/shutter/{chid}/set",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": None,  # the vendor API has no stop command
            "position_topic": f"{BASE}/shutter/{chid}/position",
            "set_position_topic": f"{BASE}/shutter/{chid}/set_position",
            "position_open": 100,
            "position_closed": 0,
            "availability_topic": AVAIL_TOPIC,
            "device": DEVICE,
        }
        client.publish(
            f"{DISCOVERY_PREFIX}/cover/{NODE_ID}/shutter_{chid}/config",
            json.dumps(cfg), retain=True,
        )
    log.info("published HA discovery: %d relays, %d shutters", len(RELAY_CHIDS), len(SHUTTER_CHIDS))


class Bridge:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{NODE_ID}-bridge")
        if MQTT_USER or MQTT_PASS:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.last = {}          # chid -> last published basicvalue
        self.online = None      # last published availability
        self.poll_now = threading.Event()
        self.fail_count = 0

    # -- mqtt callbacks --
    def on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("connected to MQTT %s:%s (%s)", MQTT_HOST, MQTT_PORT, reason_code)
        client.subscribe(f"{BASE}/relay/+/set")
        client.subscribe(f"{BASE}/shutter/+/set")
        client.subscribe(f"{BASE}/shutter/+/set_position")
        publish_discovery(client)
        self.last = {}  # force republish of all states
        self.poll_now.set()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode().strip()
        try:
            parts = msg.topic.split("/")
            kind, chid, action = parts[-3], int(parts[-2]), parts[-1]
            if kind == "relay" and action == "set" and chid in RELAY_CHIDS:
                val = 255 if payload.upper() == "ON" else 0
                log.info("cmd relay %d -> %s", chid, payload)
                switch(chid, val)
            elif kind == "shutter" and action == "set" and chid in SHUTTER_CHIDS:
                val = {"OPEN": 99, "CLOSE": 0}.get(payload.upper())
                if val is None:
                    raise ValueError(f"unsupported shutter command {payload!r}")
                log.info("cmd shutter %d -> %s", chid, payload)
                switch(chid, val)
            elif kind == "shutter" and action == "set_position" and chid in SHUTTER_CHIDS:
                pos = max(0, min(100, int(float(payload))))
                val = round(pos * 99 / 100)
                log.info("cmd shutter %d position -> %d%% (val %d)", chid, pos, val)
                switch(chid, val)
            else:
                return
            # quick refresh so HA state converges fast
            threading.Timer(1.0, self.poll_now.set).start()
        except Exception:
            log.exception("command failed (topic=%s payload=%r)", msg.topic, payload)

    # -- state publishing --
    def set_availability(self, online):
        if online != self.online:
            self.client.publish(AVAIL_TOPIC, "online" if online else "offline", retain=True)
            self.online = online
            log.info("availability -> %s", "online" if online else "offline")

    def publish_states(self, channels):
        for chid in RELAY_CHIDS:
            v = channels.get(chid)
            if v is not None and v != self.last.get(chid):
                self.client.publish(
                    f"{BASE}/relay/{chid}/state", "ON" if v > 0 else "OFF", retain=True
                )
                self.last[chid] = v
        for chid in SHUTTER_CHIDS:
            v = channels.get(chid)
            if v is not None and v != self.last.get(chid):
                self.client.publish(
                    f"{BASE}/shutter/{chid}/position", str(round(v * 100 / 99)), retain=True
                )
                self.last[chid] = v

    def run(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            try:
                channels = get_channels()
                self.fail_count = 0
                self.set_availability(True)
                self.publish_states(channels)
            except Exception as e:
                self.fail_count += 1
                log.warning("poll failed (%d): %s", self.fail_count, e)
                if self.fail_count >= 3:
                    self.set_availability(False)
            self.poll_now.wait(timeout=POLL_INTERVAL)
            self.poll_now.clear()
        self.client.publish(AVAIL_TOPIC, "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()


if __name__ == "__main__":
    log.info("Gerber Prime 6000 MQTT bridge starting (device %s, broker %s:%s, poll %.0fs)",
             GERBER_HOST, MQTT_HOST, MQTT_PORT, POLL_INTERVAL)
    log.info("relays: %s | shutters: %s | base topic: %s", RELAY_CHIDS, SHUTTER_CHIDS, BASE)
    Bridge().run()
