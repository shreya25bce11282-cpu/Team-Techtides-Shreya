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
from datetime import datetime, timedelta

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
    "outdoor_temperature": 25.0,  # from weather API
    "humidity": 50.0,             # outdoor humidity, from weather API
    "fan_on": False,
    "ac_level": "off",       # "off" | "low" | "medium" | "max"
    "light_on": False,
    "last_occupancy_update": None,  # from your radar/camera teammates
    "last_weather_update": None,    # from the weather API
    "weather_error": None,          # so the dashboard can show "weather unreachable"
    "override": {
        "active": False,
        "device": None,
        "expires_at": None,
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
        print("[WEATHER] No API key loaded -- is .env present with OPENWEATHER_API_KEY set?")
        return

    try:
        resp = requests.get(WEATHER_URL, params=WEATHER_PARAMS, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        with state_lock:
            state["outdoor_temperature"] = float(data["main"]["temp"])
            state["humidity"] = float(data["main"]["humidity"])
            state["last_weather_update"] = datetime.utcnow().isoformat()
            state["weather_error"] = None

        print(f"[WEATHER] outdoor {state['outdoor_temperature']}°C, {state['humidity']}% humidity")

    except Exception as e:
        # keep the LAST KNOWN reading rather than zeroing out -- a stale
        # value is safer than a fake sudden drop that could trip the AC/fan logic
        with state_lock:
            state["weather_error"] = str(e)
        print(f"[WEATHER] fetch failed: {e}")


def weather_loop():
    while True:
        fetch_weather()
        time.sleep(WEATHER_POLL_SECONDS)


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
            now = datetime.utcnow()

            ov = state["override"]
            if ov["active"] and ov["expires_at"] is not None:
                if now >= datetime.fromisoformat(ov["expires_at"]):
                    print(f"[OVERRIDE] {ov['device']} override expired, reverting to auto")
                    ov["active"] = False
                    ov["device"] = None
                    ov["expires_at"] = None

            override_device = state["override"]["device"] if state["override"]["active"] else None

            # FAN: temperature hysteresis, now using the real INDOOR reading,
            # gated by occupancy
            if override_device != "fan":
                if state["occupant_count"] == 0:
                    new_fan_state = False
                elif state["indoor_temperature"] > 26.0:
                    new_fan_state = True
                elif state["indoor_temperature"] < 24.0:
                    new_fan_state = False
                else:
                    new_fan_state = state["fan_on"]

                if new_fan_state != state["fan_on"]:
                    state["fan_on"] = new_fan_state
                    set_relay("fan", new_fan_state)

            # AC: indoor/outdoor diff rule.
            # If indoor and outdoor temps meaningfully differ -> force AC off.
            # If they DON'T differ -> do nothing, leave ac_level exactly as-is.
            # NOTE: as written, this rule can only ever turn AC off or leave it
            # alone -- it never turns AC on by itself. Combine with occupancy
            # scaling (ac_level_for) below if you want it to also turn on.
            if override_device != "ac":
                diff = abs(state["indoor_temperature"] - state["outdoor_temperature"])
                if diff > TEMP_DIFF_THRESHOLD:
                    if state["ac_level"] != "off":
                        state["ac_level"] = "off"
                        set_relay("ac", False)
                # else: diff is small -> "do nothing", ac_level unchanged

            # LIGHT: occupancy only
            if override_device != "light":
                new_light_state = state["occupant_count"] > 0
                if new_light_state != state["light_on"]:
                    state["light_on"] = new_light_state
                    set_relay("light", new_light_state)

        time.sleep(2)


# ----------------------------------------------------------------------
# 6. API ROUTES
# ----------------------------------------------------------------------

@app.route("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")


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
    data = request.get_json(force=True)

    with state_lock:
        if "occupant_count" in data:
            state["occupant_count"] = int(data["occupant_count"])
        state["last_occupancy_update"] = datetime.utcnow().isoformat()

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
    data = request.get_json(force=True)
    device = data.get("device")
    desired_state = bool(data.get("state", False))
    duration = int(data.get("duration_minutes", 30))

    if device not in RELAY_PINS:
        return jsonify({"ok": False, "error": "unknown device"}), 400

    with state_lock:
        expires_at = datetime.utcnow() + timedelta(minutes=duration)
        state["override"] = {
            "active": True,
            "device": device,
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
    with state_lock:
        state["override"] = {"active": False, "device": None, "expires_at": None}
    return jsonify({"ok": True, "state": state})


# ----------------------------------------------------------------------
# 7. START EVERYTHING
# ----------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Relay hardware mode: {HARDWARE_MODE}")
    print(f"BMP280 hardware mode: {BMP_HARDWARE_MODE}")
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    app.run(debug=True, host="0.0.0.0", port=5000)
