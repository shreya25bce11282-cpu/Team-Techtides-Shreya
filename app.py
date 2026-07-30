"""
AEGIS-Eco Backend
==================
This is the "brain" of the system. It does three jobs:

1. Holds the current state of the room in memory (occupants, indoor temp,
   outdoor temp, humidity, what each device is doing).
2. Runs the decision logic (fan hysteresis + indoor/outdoor AC rule +
   occupancy scaling) on a background loop, and flips relays accordingly.
3. Serves the dashboard + a small JSON API so:
   - your radar/camera teammates' script pushes in occupant_count
   - the BMP280 (wired directly to THIS Pi) gives indoor temperature
   - a weather API gives outdoor temperature + humidity automatically
   - the frontend pulls state out to display it

Run it with:  python app.py
Then open:    http://127.0.0.1:5000
"""

import os
import random
import threading
import time
from datetime import datetime, timedelta, UTC

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()  # reads the .env file in this folder and loads it into the environment

app = Flask(__name__)

# ----------------------------------------------------------------------
# 0. WEATHER CONFIG (outdoor temperature + humidity)
# ----------------------------------------------------------------------
# The API key itself lives in a separate .env file (NOT this file, and NOT
# committed to GitHub) -- see .env in this same folder:
#   OPENWEATHER_API_KEY=your_key_here
# Get a free key at https://openweathermap.org/api if you ever need a new one.
# Free-tier keys can take up to ~1 hour to activate after signup.

WEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

LATITUDE = 23.073212    
LONGITUDE = 76.855446
WEATHER_POLL_SECONDS = 600  # weather doesn't change fast -- every 10 min is plenty

WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
WEATHER_PARAMS = {
    "lat": LATITUDE,
    "lon": LONGITUDE,
    "appid": WEATHER_API_KEY,
    "units": "metric",  # so temp comes back in °C, not Kelvin
}

# How big a gap between indoor and outdoor temp counts as "there IS a
# difference"? A small buffer (not 0.0) avoids the AC flicking off from
# normal sensor noise when the two readings are basically the same.
TEMP_DIFF_THRESHOLD = 1.0  # °C

# Fan hysteresis thresholds -- named here (not just inline in control_loop)
# so /api/config can expose them and the dashboard doesn't have to hardcode
# its own copy that could drift out of sync.
FAN_ON_TEMP = 26.0   # °C, fan switches on above this
FAN_OFF_TEMP = 24.0  # °C, fan switches off below this

# Toggle Flask's debug mode via .env -- defaults to OFF. Debug mode exposes
# an interactive Python console in the browser on unhandled errors, which
# you do NOT want reachable on venue WiFi during a demo. Set FLASK_DEBUG=true
# in your .env only for your own local development.
DEBUG_MODE = os.getenv("FLASK_DEBUG", "false").lower() == "true"


# ----------------------------------------------------------------------
# 1. RELAY CONTROL LAYER
# ----------------------------------------------------------------------
# gpiozero on the real Pi, printed mock otherwise -- so you can develop
# this whole file on a laptop with zero hardware attached.

RELAY_PINS = {"fan": 17, "ac": 27, "light": 22}

try:
    from gpiozero import OutputDevice
    _relays = {name: OutputDevice(pin) for name, pin in RELAY_PINS.items()}
    HARDWARE_MODE = True
except Exception:
    _relays = {}
    HARDWARE_MODE = False


def set_relay(device: str, on: bool):
    if HARDWARE_MODE:
        if on:
            _relays[device].on()
        else:
            _relays[device].off()
    else:
        print(f"[MOCK RELAY] {device.upper()} -> {'ON' if on else 'OFF'}")


# ----------------------------------------------------------------------
# 2. INDOOR TEMPERATURE SENSOR (BMP280)
# ----------------------------------------------------------------------
# Wiring (I2C): VCC -> 3.3V, GND -> GND, SCL -> GPIO3 (pin 5), SDA -> GPIO2 (pin 3)
# Before this works on the Pi: sudo raspi-config -> Interface Options -> I2C -> enable, then reboot.
# Install:  pip install smbus2 bmp280

try:
    from smbus2 import SMBus
    from bmp280 import BMP280
    _bmp_bus = SMBus(1)
    _bmp280 = BMP280(i2c_dev=_bmp_bus)
    BMP_HARDWARE_MODE = True
except Exception:
    _bmp280 = None
    BMP_HARDWARE_MODE = False


def read_indoor_temperature() -> float:
    """Reads indoor temp from the BMP280. Falls back to a mock reading with no hardware attached."""
    if BMP_HARDWARE_MODE:
        return round(_bmp280.get_temperature(), 1)
    else:
        # mock mode: random-ish indoor reading so you can test the diff logic
        # end-to-end before the sensor is wired up
        return round(random.uniform(22.0, 29.0), 1)


# ----------------------------------------------------------------------
# 3. SHARED STATE
# ----------------------------------------------------------------------

state_lock = threading.Lock()

state = {
    "occupant_count": 0,
    "indoor_temperature": 25.0,   # from BMP280, on this Pi
    "outdoor_temperature": 25.0,  # from weather API, or mirrored from indoor if unavailable
    "humidity": 50.0,             # outdoor humidity, from weather API
    "weather_available": False,   # true once we've had a successful weather fetch
    "fan_on": False,
    "ac_level": "off",       # "off" | "low" | "medium" | "max"
    "light_on": False,
    "last_occupancy_update": None,  # from your radar/camera teammates
    "last_weather_update": None,    # from the weather API
    "weather_error": None,          # so the dashboard can show "weather unreachable"
    "overrides": {
        "fan": {"active": False, "expires_at": None},
        "ac": {"active": False, "expires_at": None},
        "light": {"active": False, "expires_at": None},
    },
    "savings": {
        "tracking_since": datetime.now(UTC).isoformat(),
        "energy_used_kwh": 0.0,
        "energy_baseline_kwh": 0.0,   # what it WOULD have used if everything ran 24/7
        "energy_saved_kwh": 0.0,
        "cost_saved": 0.0,            # ₹, matches dashboard.html's sv.cost_saved
    },
}


# ----------------------------------------------------------------------
# 4. WEATHER POLLING LOOP (outdoor conditions only)
# ----------------------------------------------------------------------

def fetch_weather():
    """Hits OpenWeatherMap once and updates state. Never crashes the app if it's down."""
    if not WEATHER_API_KEY:
        with state_lock:
            state["weather_error"] = "No OPENWEATHER_API_KEY found -- check your .env file"
            state["weather_available"] = False
        print("[WEATHER] No API key loaded -- is .env present with OPENWEATHER_API_KEY set?")
        return

    try:
        resp = requests.get(WEATHER_URL, params=WEATHER_PARAMS, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        with state_lock:
            state["outdoor_temperature"] = float(data["main"]["temp"])
            state["humidity"] = float(data["main"]["humidity"])
            state["last_weather_update"] = datetime.now(UTC).isoformat()
            state["weather_error"] = None
            state["weather_available"] = True

        print(f"[WEATHER] outdoor {state['outdoor_temperature']}°C, {state['humidity']}% humidity")

    except Exception as e:
        # don't zero out -- the control loop will mirror indoor temp onto
        # outdoor_temperature every tick while weather_available is False,
        # so the dashboard always has a real, live number to show
        with state_lock:
            state["weather_error"] = str(e)
            state["weather_available"] = False
        print(f"[WEATHER] fetch failed: {e}")


def weather_loop():
    while True:
        fetch_weather()
        time.sleep(WEATHER_POLL_SECONDS)


# ----------------------------------------------------------------------
# 4b. SAVINGS TRACKER
# ----------------------------------------------------------------------
# Compares actual energy used against a baseline of "everything ran
# continuously since tracking started" to estimate money saved. Feeds the
# "Financial Savings" card on the dashboard.

# Rough wattage estimates -- adjust to match whatever you actually demo with.
APPLIANCE_WATTS = {"fan": 75, "light": 40, "ac": 1500}  # ac watts = its "max" draw
AC_LEVEL_FACTOR = {"off": 0.0, "low": 0.3, "medium": 0.6, "max": 1.0}
ENERGY_PRICE_PER_KWH = 8.0  # ₹ per unit -- change to your local electricity tariff
TICK_SECONDS = 2  # must match the time.sleep() at the bottom of control_loop

# Internal bookkeeping -- NOT sent to the frontend directly, only the
# computed results in state["savings"] are.
_on_seconds = {"fan": 0.0, "light": 0.0, "ac_weighted": 0.0}


def update_savings(now: datetime):
    """Called once per control_loop tick, while state_lock is already held."""
    if state["fan_on"]:
        _on_seconds["fan"] += TICK_SECONDS
    if state["light_on"]:
        _on_seconds["light"] += TICK_SECONDS
    _on_seconds["ac_weighted"] += TICK_SECONDS * AC_LEVEL_FACTOR[state["ac_level"]]

    tracking_since = datetime.fromisoformat(state["savings"]["tracking_since"])
    elapsed_seconds = (now - tracking_since).total_seconds()
    if elapsed_seconds <= 0:
        return

    baseline_kwh = (
        (APPLIANCE_WATTS["fan"] + APPLIANCE_WATTS["light"] + APPLIANCE_WATTS["ac"])
        * (elapsed_seconds / 3600)
    ) / 1000

    used_kwh = (
        APPLIANCE_WATTS["fan"] * (_on_seconds["fan"] / 3600)
        + APPLIANCE_WATTS["light"] * (_on_seconds["light"] / 3600)
        + APPLIANCE_WATTS["ac"] * (_on_seconds["ac_weighted"] / 3600)
    ) / 1000

    saved_kwh = baseline_kwh - used_kwh

    state["savings"]["energy_used_kwh"] = round(used_kwh, 4)
    state["savings"]["energy_baseline_kwh"] = round(baseline_kwh, 4)
    state["savings"]["energy_saved_kwh"] = round(saved_kwh, 4)
    state["savings"]["cost_saved"] = round(saved_kwh * ENERGY_PRICE_PER_KWH, 2)


def reset_savings():
    """Called by the dashboard's 'Reset Counter' button, while state_lock is held."""
    _on_seconds["fan"] = 0.0
    _on_seconds["light"] = 0.0
    _on_seconds["ac_weighted"] = 0.0
    state["savings"] = {
        "tracking_since": datetime.now(UTC).isoformat(),
        "energy_used_kwh": 0.0,
        "energy_baseline_kwh": 0.0,
        "energy_saved_kwh": 0.0,
        "cost_saved": 0.0,
    }


# ----------------------------------------------------------------------
# 5. DECISION LOGIC ("the brain")
# ----------------------------------------------------------------------

def ac_level_for(occupant_count: int) -> str:
    if occupant_count <= 0:
        return "off"
    elif occupant_count <= 5:
        return "low"
    elif occupant_count <= 20:
        return "medium"
    else:
        return "max"


def control_loop():
    while True:
        # do the I2C read BEFORE grabbing the lock, so the lock is held for
        # as short a time as possible
        indoor_temp = read_indoor_temperature()

        with state_lock:
            state["indoor_temperature"] = indoor_temp

            # If we have no working weather feed, mirror outdoor to indoor so
            # there's always a real, live number on the dashboard (never a
            # stale default) -- and the diff naturally becomes 0, so the
            # AC diff-rule below won't misfire from missing data.
            if not state["weather_available"]:
                state["outdoor_temperature"] = indoor_temp

            now = datetime.now(UTC)

            # Each device tracks its own override independently now -- check
            # every one for expiry, rather than one shared override slot.
            for dev_name, ov in state["overrides"].items():
                if ov["active"] and ov["expires_at"] is not None:
                    if now >= datetime.fromisoformat(ov["expires_at"]):
                        print(f"[OVERRIDE] {dev_name} override expired, reverting to auto")
                        ov["active"] = False
                        ov["expires_at"] = None

            # FAN: temperature hysteresis, now using the real INDOOR reading,
            # gated by occupancy
            if not state["overrides"]["fan"]["active"]:
                if state["occupant_count"] == 0:
                    new_fan_state = False
                elif state["indoor_temperature"] > FAN_ON_TEMP:
                    new_fan_state = True
                elif state["indoor_temperature"] < FAN_OFF_TEMP:
                    new_fan_state = False
                else:
                    new_fan_state = state["fan_on"]

                if new_fan_state != state["fan_on"]:
                    state["fan_on"] = new_fan_state
                    set_relay("fan", new_fan_state)

            # AC: combines your indoor/outdoor diff rule with occupancy-based
            # dynamic load scaling.
            # - If indoor/outdoor temps meaningfully differ -> force AC off.
            # - Otherwise -> scale AC to occupant count (ac_level_for), so it
            #   can actually turn ON, not just off. This restores the "Dynamic
            #   Load Scaling" behavior from the pitch deck, which the diff
            #   rule alone couldn't do by itself.
            if not state["overrides"]["ac"]["active"]:
                diff = abs(state["indoor_temperature"] - state["outdoor_temperature"])
                if diff > TEMP_DIFF_THRESHOLD:
                    new_ac_level = "off"
                else:
                    new_ac_level = ac_level_for(state["occupant_count"])

                if new_ac_level != state["ac_level"]:
                    state["ac_level"] = new_ac_level
                    set_relay("ac", new_ac_level != "off")

            # LIGHT: occupancy only
            if not state["overrides"]["light"]["active"]:
                new_light_state = state["occupant_count"] > 0
                if new_light_state != state["light_on"]:
                    state["light_on"] = new_light_state
                    set_relay("light", new_light_state)

            # SAVINGS: update the running ROI numbers using this tick's device states
            update_savings(now)

        time.sleep(TICK_SECONDS)


# ----------------------------------------------------------------------
# 6. API ROUTES
# ----------------------------------------------------------------------

@app.route("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    """
    Exposes the thresholds the decision logic actually uses, so the
    dashboard can display accurate numbers instead of keeping its own
    hardcoded copy that could silently drift out of sync.
    """
    return jsonify({
        "fan_on_temp": FAN_ON_TEMP,
        "fan_off_temp": FAN_OFF_TEMP,
        "temp_diff_threshold": TEMP_DIFF_THRESHOLD,
        "energy_price_per_kwh": ENERGY_PRICE_PER_KWH,
    })


@app.route("/api/state", methods=["GET"])
def get_state():
    with state_lock:
        return jsonify(state)


@app.route("/api/sensor-update", methods=["POST"])
def sensor_update():
    """
    Your radar/camera teammates' script POSTs here whenever it has a fresh
    occupant count.

    Expected JSON body:
    { "occupant_count": 3 }
    """
    data = request.get_json(silent=True)
    if not data or "occupant_count" not in data:
        return jsonify({"ok": False, "error": "expected a JSON body with 'occupant_count'"}), 400

    try:
        occupant_count = int(data["occupant_count"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "occupant_count must be a whole number"}), 400

    if occupant_count < 0:
        return jsonify({"ok": False, "error": "occupant_count can't be negative"}), 400

    with state_lock:
        state["occupant_count"] = occupant_count
        state["last_occupancy_update"] = datetime.now(UTC).isoformat()

    return jsonify({"ok": True, "state": state})


@app.route("/api/override", methods=["POST"])
def override():
    """
    Manual control from the dashboard, with an auto-revert timer.

    Expected JSON body:
    {
        "device": "fan",          # "fan" | "ac" | "light"
        "state": true,
        "duration_minutes": 30
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "expected a JSON body"}), 400

    device = data.get("device")
    if device not in RELAY_PINS:
        return jsonify({"ok": False, "error": f"device must be one of {list(RELAY_PINS)}"}), 400

    desired_state = bool(data.get("state", False))

    try:
        duration = int(data.get("duration_minutes", 30))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "duration_minutes must be a whole number"}), 400

    if duration <= 0:
        return jsonify({"ok": False, "error": "duration_minutes must be positive"}), 400

    with state_lock:
        expires_at = datetime.now(UTC) + timedelta(minutes=duration)
        state["overrides"][device] = {
            "active": True,
            "expires_at": expires_at.isoformat(),
        }

        if device == "fan":
            state["fan_on"] = desired_state
        elif device == "light":
            state["light_on"] = desired_state
        elif device == "ac":
            state["ac_level"] = "max" if desired_state else "off"

        set_relay(device, desired_state)

    return jsonify({"ok": True, "state": state})


@app.route("/api/override/cancel", methods=["POST"])
def cancel_override():
    """
    Expected JSON body:
    { "device": "fan" }   # "fan" | "ac" | "light"
    """
    data = request.get_json(silent=True)
    device = data.get("device") if data else None
    if device not in RELAY_PINS:
        return jsonify({"ok": False, "error": f"device must be one of {list(RELAY_PINS)}"}), 400

    with state_lock:
        state["overrides"][device] = {"active": False, "expires_at": None}
    return jsonify({"ok": True, "state": state})


@app.route("/api/savings/reset", methods=["POST"])
def savings_reset():
    """Called by the dashboard's 'Reset Counter' button."""
    with state_lock:
        reset_savings()
    return jsonify({"ok": True, "state": state})


# ----------------------------------------------------------------------
# 7. START EVERYTHING
# ----------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Relay hardware mode: {HARDWARE_MODE}")
    print(f"BMP280 hardware mode: {BMP_HARDWARE_MODE}")
    if DEBUG_MODE:
        print("[WARNING] Flask debug mode is ON -- do not use this on venue WiFi during a demo. "
              "Set FLASK_DEBUG=false (or remove it) in .env before showing this to judges.")
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    app.run(debug=DEBUG_MODE, host="0.0.0.0", port=5000)