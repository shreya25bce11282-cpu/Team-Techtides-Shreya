# AEGIS-Eco

**A privacy-first edge device that watches a room breathe, and only spends energy when someone's actually there.**

Built by **Team Techtides** for SOCF 2.0.

---

## The problem

Empty classrooms and offices burn electricity for hours because dumb PIR sensors either miss people sitting still (reading, in an exam) or can't tell the difference between 1 person and 40 — so buildings run AC at one fixed setting no matter who's in the room.

## The idea

AEGIS-Eco fuses a 24GHz mmWave radar (which can detect breathing, not just movement) with an Edge AI camera pipeline to know *exactly* how many people are in a room, then scales lighting, fans, and AC to match — entirely on-device, with zero video ever leaving the room.

This repository holds the **backend brain and web dashboard**: the Flask service that turns sensor readings into device decisions, and the live console that lets admins watch it happen.

---

## How it thinks

```
 24GHz Radar ──┐
               ├─► occupant_count ──┐
 YOLOv8 Camera ─┘                   │
                                     ▼
 BMP280 (indoor temp) ──────► ┌─────────────┐        ┌──────────────┐
                               │  Flask      │───────►│ Relays        │
 OpenWeatherMap (outdoor) ───► │  Brain      │        │ fan/ac/light  │
                               └─────────────┘        └──────────────┘
                                     │
                                     ▼
                          ┌────────────────────┐
                          │  Web Dashboard      │
                          │  live state, override,
                          │  ROI savings tracker │
                          └────────────────────┘
```

**Decision rules:**

| Device | Logic |
|---|---|
| **Fan** | Hysteresis on indoor temperature — ON above 26°C, stays on until it drops below 24°C *or* the room empties. The gap between thresholds stops it flickering. |
| **AC** | If indoor and outdoor temperature differ by more than 1°C, force AC off (no point fighting the outside air). Otherwise, scale by occupant count — off for 0, low for 1–5, medium for 6–20, max for 20+. |
| **Light** | On whenever `occupant_count > 0`. |
| **Any device** | Can be force-overridden from the dashboard, with an auto-revert timer so someone forgetting to switch it back doesn't waste power all night. |

**Sensor-less by design, when it has to be:** every input has a mock/fallback path, so the whole system runs and shows real numbers on a laptop with zero hardware attached — useful for development and for judges who want to see the logic work without wiring anything up.

---

## Features

- 🌡️ **Dual temperature sourcing** — real indoor reading from a BMP280, real outdoor reading from a live weather API, compared every 2 seconds
- 🧠 **Hysteresis + occupancy-aware automation** — no flicker, no wasted cycling
- 💡 **Manual override with auto-revert** — take control from the dashboard, it hands itself back automatically
- 💰 **Live ROI tracker** — running ₹ estimate of money saved vs. an "everything ran 24/7" baseline, resettable on demand
- 🔒 **Zero-cloud** — everything runs on a local Flask server, no data leaves the device
- 🧪 **Fully demoable without hardware** — every sensor has a graceful mock fallback

---

## Tech stack

| Layer | Technology |
|---|---|
| Core hardware | Raspberry Pi 5 (4GB) |
| Indoor sensor | BMP280 (I2C) |
| Outdoor data | OpenWeatherMap API |
| Occupancy input | 24GHz mmWave radar + Pi Camera v3 / YOLOv8-Nano *(external module, feeds this backend)* |
| Backend | Python 3.x, Flask |
| Frontend | HTML5, CSS3, vanilla JavaScript |
| Actuation | 4-channel relay board (5V/12V) via `gpiozero` |

---

## Getting started

### 1. Install dependencies
```bash
pip install flask requests python-dotenv
```
On the actual Pi, also: `pip install gpiozero smbus2 bmp280`

### 2. Set up your weather API key
Copy `.env.example` to `.env` and fill in your key:
```
OPENWEATHER_API_KEY=your_key_here
```
Get a free key at [openweathermap.org/api](https://openweathermap.org/api) (can take up to an hour to activate). `.env` is gitignored — never commit your real key.

### 3. Set your location
In `app.py`, update:
```python
LATITUDE = 23.0225
LONGITUDE = 72.5714
```

### 4. Run it
```bash
python app.py
```
In a second terminal, simulate occupancy data (until the real radar/camera pipeline is wired in):
```bash
python mock_sensor.py
```

### 5. Open the dashboard
```
http://127.0.0.1:5000
```

---

## Wiring the BMP280 (I2C)

| BMP280 pin | Pi pin |
|---|---|
| VCC | 3.3V |
| GND | GND |
| SCL | GPIO3 (pin 5) |
| SDA | GPIO2 (pin 3) |

Enable I2C first: `sudo raspi-config` → Interface Options → I2C → Enable → reboot.

No sensor wired up yet? No problem — `app.py` automatically falls back to a mock reading so development and testing aren't blocked.

---

## API reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the dashboard |
| `/api/state` | GET | Full current state — occupancy, temps, device states, savings |
| `/api/sensor-update` | POST | Radar/camera pipeline pushes `{ "occupant_count": N }` here |
| `/api/override` | POST | Force a device on/off — `{ "device", "state", "duration_minutes" }` |
| `/api/override/cancel` | POST | Cancel an active override early |
| `/api/savings/reset` | POST | Zero out the ROI savings counter |

---

## Project structure

```
.
├── app.py             # Flask backend — state, decision logic, API
├── dashboard.html      # Live web console
├── mock_sensor.py      # Simulates occupancy data for dev/testing
├── .env.example         # Template for your weather API key
├── .gitignore
└── README.md
```

---

## Known limitations

- Occupancy count depends on the radar/camera pipeline, which lives outside this repo — this backend simply trusts whatever `/api/sensor-update` sends it.
- Outdoor weather updates every 10 minutes (by design — weather doesn't change fast, and it keeps API usage well within free-tier limits).
- Appliance wattages used for the savings estimate are reasonable approximations, not measured values — edit `APPLIANCE_WATTS` in `app.py` to match your actual hardware for a more accurate number.

---

## Team Techtides

Built for SOCF 2.0.
