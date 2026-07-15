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
