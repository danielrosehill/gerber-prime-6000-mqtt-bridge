# Gerber Prime 6000 → MQTT bridge (Home Assistant integration)

A small Python service that puts a **Gerber Prime 6000-series** smart-home controller
(relay / roller-shutter DIN-rail box, phone app "Home Mate 2", Wi-Fi module **PSC05**) into
**Home Assistant** — locally, with **no vendor cloud** and no extra hardware.

It bridges the device's undocumented local HTTP API (`/sdk.cgi`) to **MQTT**, and publishes
**Home Assistant MQTT discovery** messages, so HA auto-creates one device with:

- one `switch` entity per relay channel
- one **positional** `cover` entity (device class `shutter`) per roller-shutter channel

The API itself (endpoints, encoding, gotchas, how to get the unit onto your LAN) is documented
in the sister repo: **[gerber-prime-6000-local-api](https://github.com/danielrosehill/gerber-prime-6000-local-api)**.
Read that first if your unit isn't reachable over your LAN yet.

> ⚠️ Unofficial, community-maintained, not affiliated with Gerber. Commands actuate real
> relays and shutters — use at your own risk.

**Write-up:** [Gerber Prime 6000 in Home Assistant: local control, no cloud](https://blog.danielrosehill.com/posts/gerber-prime-6000-home-assistant-local-control-xwjj6i/) — the full step-by-step walkthrough.

## How it works

```
Home Assistant ──MQTT──▶ broker ◀──MQTT── this bridge ──HTTP──▶ Gerber (/sdk.cgi)
   (discovery, state, commands)             (poll + command translation)
```

- Polls `get_all_device` every `POLL_INTERVAL` seconds (default 10) and publishes state on
  change (retained).
- Subscribes to command topics and translates them to the device's `switch` command
  (relays: val `0`/`255`; shutters: **positional** val `0–99` — the controller does the
  time-based travel itself using its configured travel time).
- Publishes availability (`online`/`offline`) with an MQTT Last Will; marks the device
  offline after 3 consecutive failed polls.
- After any command it re-polls after ~1 s so HA state converges quickly.

### Why MQTT instead of HA REST/template YAML?

A `rest_command` + `command_line` + template-entity approach works, but it's a lot of YAML,
polls from inside HA, and recent HA versions dropped the legacy template switch/cover
platform syntax. MQTT discovery gives you a proper device page, push state, availability,
and zero HA configuration.

### Known limitation: no stop

The vendor API has **no stop command** for shutters (verified against the web UI's JS — the
UI itself only offers a 0–99 position slider). The HA cover therefore has open/close/
set-position but no stop button. Position updates when travel completes, so HA lags the
physical shutter by the travel time (~20 s typical).

### The shutter scale is inverted relative to Home Assistant

The controller counts a shutter as `basicvalue` **0 = fully open, 99 = fully closed**.
Home Assistant's cover position is the opposite: **100 = open, 0 = closed**. The bridge
is the single place that knows about this — `device_to_ha()` / `ha_to_device()` invert in
both directions, so MQTT and discovery are plain HA convention
(`position_open: 100`, `position_closed: 0`) and need no adjustment.

Passing `basicvalue` through unconverted breaks all three shutter paths at once:
position reporting reads inverted, `payload_open` drives the shutter closed, and
`set_position` to 100 % sends `99` (fully closed). The mapping round-trips exactly —
`ha_to_device(device_to_ha(v)) == v` for every `v` in 0–99.

Note that at `current_position: 4` HA still reports the cover as `open`; HA's MQTT cover
only says `closed` at position exactly 0. That is standard HA behaviour, not a bridge bug.

### Availability must be re-asserted after every MQTT reconnect

`availability` is published retained and is also the LWT, and the bridge only publishes it
on *change* (to keep the topic quiet). Those two facts combine into a trap: when the broker
restarts, the LWT leaves a retained `offline` on the topic, the bridge reconnects, but its
in-memory "last published availability" still says `online` — so it never corrects the
topic. Every entity then sits **unavailable in Home Assistant indefinitely while the bridge
is healthy**, polling the device and publishing state changes that HA ignores.

`on_connect` therefore resets `self.online = None`, which forces the next successful poll to
republish `online`. If you refactor availability handling, keep that reset.

Diagnosing this from the HA side is misleading — the entities look like a dead device. Check
the retained topic directly before suspecting the controller:

```bash
mosquitto_sub -h <broker> -u <user> -P <pass> -t '<base>/#' -v -W 5
```

`availability offline` alongside plausible, current relay states is this bug, not an outage.

## Setup

Requirements: the controller reachable on your LAN (static DHCP reservation recommended —
see the sister repo for the STA-mode procedure and its pitfalls), an MQTT broker that your
Home Assistant MQTT integration is already connected to, and Docker (or just Python 3.10+
with `paho-mqtt>=2.0`).

1. Find your channel layout:

   ```bash
   # from the sister repo
   python gerber_client.py --host <CONTROLLER_IP> get-devices
   ```

   Channels with `ctrltype 23` are relays, `26` are roller shutters. Note the `uid` too.

2. Configure and run:

   ```bash
   cp .env.example .env               # edit: controller IP, broker, channel ids
   cp docker-compose.example.yml docker-compose.yml
   docker compose up -d --build
   ```

   Or without Docker: `pip install "paho-mqtt>=2.0"` and run `gerber_mqtt_bridge.py` with the
   same variables in the environment.

3. Open Home Assistant → Settings → Devices & Services → MQTT. A device (default name
   "Gerber Prime 6000") appears with all entities. Rename the generic "Relay N" entities to
   whatever each channel actually switches.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `GERBER_HOST` | `10.10.10.254` | Controller IP (the default is the vendor SoftAP address — set your LAN IP) |
| `GERBER_USER` / `GERBER_PASS` | `admin` / `888888` | HTTP Basic auth (vendor defaults) |
| `GERBER_UID` | `257` | Device `uid` from `get_all_device` |
| `RELAY_CHIDS` | `1,2,3,4,5,6` | Comma-separated relay channel ids |
| `SHUTTER_CHIDS` | `7` | Comma-separated shutter channel ids |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | Broker |
| `MQTT_USER` / `MQTT_PASS` | *(empty)* | Broker auth (anonymous if empty) |
| `MQTT_BASE_TOPIC` | `gerber/gps6000` | Base for state/command topics (also used as the HA node id — keep it unique per unit) |
| `DISCOVERY_PREFIX` | `homeassistant` | HA discovery prefix |
| `DEVICE_NAME` | `Gerber Prime 6000` | HA device name |
| `POLL_INTERVAL` | `10` | State poll interval (seconds) |

Running **two or more units**: run one bridge container per unit with distinct
`MQTT_BASE_TOPIC` (and `DEVICE_NAME`) values.

## Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `<base>/availability` | pub, retained | `online` / `offline` (LWT) |
| `<base>/relay/<chid>/state` | pub, retained | `ON` / `OFF` |
| `<base>/relay/<chid>/set` | sub | `ON` / `OFF` |
| `<base>/shutter/<chid>/position` | pub, retained | `0`–`100` (100 = open) |
| `<base>/shutter/<chid>/set` | sub | `OPEN` / `CLOSE` |
| `<base>/shutter/<chid>/set_position` | sub | `0`–`100` |

## License

MIT — see [LICENSE](LICENSE).
