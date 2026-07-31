"""
AEGIS-Eco Backend
==================
This is the "brain" of the system. It does three jobs:

1. Holds the current state of the room in memory (occupants, indoor temp,
   outdoor temp, humidity, what each device is doing).
2. Runs the decision logic (fan hysteresis + occupancy scaling) on a 
   background loop, and flips relays accordingly.
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
WEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

LATITUDE = 23.073212    
LONGITUDE = 76.855446
WEATHER_POLL_SECONDS = 600  

WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
WEATHER_PARAMS = {
    "lat": LATITUDE,
    "lon": LONGITUDE,
    "appid": WEATHER_API_KEY,
    "units": "metric",  
}

TEMP_DIFF_THRESHOLD = 1.0  # °C

# Fan hysteresis thresholds 
FAN_ON_TEMP = 26.0   # °C, fan switches on above this
FAN_OFF_TEMP = 24.0  # °C, fan switches off below this

# How long to keep treating the room as "occupied" after the sensor reports
# 0 people, before actually cutting fan/lights. The camera node posts a fresh
# occupant_count from a SINGLE frame every ~2s, so any one frame with a missed
# detection (motion blur, someone turning away, brief occlusion) would
# otherwise instantly read as "room empty" and flick the fan/lights off, then
# back on next frame. This grace window smooths that out.
OCCUPANCY_GRACE_SECONDS = 16.0

# The BMP280 gives a fresh, individually-noisy reading every control-loop tick
# (every TICK_SECONDS). A single noisy sample right at the FAN_ON_TEMP /
# FAN_OFF_TEMP edge (self-heating drift, a passing draft, sensor jitter) would
# otherwise be enough to flip the fan for a moment even though the room hasn't
# really changed temperature. TEMP_SMOOTHING_ALPHA controls an exponential
# moving average used ONLY for the fan decision (the dashboard still shows the
# live raw reading) -- lower alpha = smoother/slower to react, higher = snappier.
TEMP_SMOOTHING_ALPHA = 0.2

DEBUG_MODE = os.getenv("FLASK_DEBUG", "false").lower() == "true"


# ----------------------------------------------------------------------
# 1. RELAY CONTROL LAYER
# ----------------------------------------------------------------------
# Updated relay pins for Fan, Light 1, and Light 2. Adjust pins as needed!
RELAY_PINS = {"fan": 17, "light_1": 27, "light_2": 22}

# Most common relay boards (the cheap blue 1/2/4-channel modules) are
# ACTIVE-LOW: the relay energizes (appliance ON) when the GPIO pin goes LOW,
# not HIGH. If your board is the opposite (appliance turns ON when you
# expect OFF, after this fix), flip this one line to True and restart.
RELAY_ACTIVE_HIGH = False

try:
    from gpiozero import OutputDevice
    _relays = {
        name: OutputDevice(pin, active_high=RELAY_ACTIVE_HIGH, initial_value=False)
        for name, pin in RELAY_PINS.items()
    }
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
    if BMP_HARDWARE_MODE:
        return round(_bmp280.get_temperature(), 1)
    else:
        return round(random.uniform(22.0, 29.0), 1)


# ----------------------------------------------------------------------
# 3. SHARED STATE
# ----------------------------------------------------------------------
state_lock = threading.Lock()

state = {
    "occupant_count": 0,
    "indoor_temperature": 25.0,   
    "outdoor_temperature": 25.0,  
    "humidity": 50.0,             
    "weather_available": False,   
    "fan_on": False,
    "light_1_on": False,
    "light_2_on": False,
    "last_occupancy_update": None,  
    "last_weather_update": None,    
    "weather_error": None,          
    "overrides": {
        "fan": {"active": False, "expires_at": None, "duration_minutes": None},
        "light_1": {"active": False, "expires_at": None, "duration_minutes": None},
        "light_2": {"active": False, "expires_at": None, "duration_minutes": None},
    },
    "savings": {
        "tracking_since": datetime.now(UTC).isoformat(),
        "energy_used_kwh": 0.0,
        "energy_baseline_kwh": 0.0,   
        "energy_saved_kwh": 0.0,
        "cost_saved": 0.0,            
    },
}


# ----------------------------------------------------------------------
# 4. WEATHER POLLING LOOP 
# ----------------------------------------------------------------------
def fetch_weather():
    if not WEATHER_API_KEY:
        with state_lock:
            state["weather_error"] = "No OPENWEATHER_API_KEY found"
            state["weather_available"] = False
        print("[WEATHER] No API key loaded")
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

    except Exception as e:
        with state_lock:
            state["weather_error"] = str(e)
            state["weather_available"] = False


def weather_loop():
    while True:
        fetch_weather()
        time.sleep(WEATHER_POLL_SECONDS)


# ----------------------------------------------------------------------
# 4b. SAVINGS TRACKER
# ----------------------------------------------------------------------
# Fixed for new devices.
APPLIANCE_WATTS = {"fan": 75, "light_1": 20, "light_2": 20} 
ENERGY_PRICE_PER_KWH = 8.0  
TICK_SECONDS = 2  

_on_seconds = {"fan": 0.0, "light_1": 0.0, "light_2": 0.0}

def update_savings(now: datetime):
    if state["fan_on"]:
        _on_seconds["fan"] += TICK_SECONDS
    if state["light_1_on"]:
        _on_seconds["light_1"] += TICK_SECONDS
    if state["light_2_on"]:
        _on_seconds["light_2"] += TICK_SECONDS

    tracking_since = datetime.fromisoformat(state["savings"]["tracking_since"])
    elapsed_seconds = (now - tracking_since).total_seconds()
    if elapsed_seconds <= 0:
        return

    baseline_kwh = (
        (APPLIANCE_WATTS["fan"] + APPLIANCE_WATTS["light_1"] + APPLIANCE_WATTS["light_2"])
        * (elapsed_seconds / 3600)
    ) / 1000

    used_kwh = (
        APPLIANCE_WATTS["fan"] * (_on_seconds["fan"] / 3600)
        + APPLIANCE_WATTS["light_1"] * (_on_seconds["light_1"] / 3600)
        + APPLIANCE_WATTS["light_2"] * (_on_seconds["light_2"] / 3600)
    ) / 1000

    saved_kwh = baseline_kwh - used_kwh

    state["savings"]["energy_used_kwh"] = round(used_kwh, 4)
    state["savings"]["energy_baseline_kwh"] = round(baseline_kwh, 4)
    state["savings"]["energy_saved_kwh"] = round(saved_kwh, 4)
    state["savings"]["cost_saved"] = round(saved_kwh * ENERGY_PRICE_PER_KWH, 2)


def reset_savings():
    _on_seconds["fan"] = 0.0
    _on_seconds["light_1"] = 0.0
    _on_seconds["light_2"] = 0.0
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
def _device_on(device: str) -> bool:
    return state[f"{device}_on"]


_last_occupied_at = None  # wall-clock time we last saw occupant_count > 0
_smoothed_indoor_temp = None  # EMA of indoor temp, used only for fan decisions


def control_loop():
    global _last_occupied_at, _smoothed_indoor_temp
    while True:
        indoor_temp = read_indoor_temperature()

        with state_lock:
            state["indoor_temperature"] = indoor_temp

            if _smoothed_indoor_temp is None:
                _smoothed_indoor_temp = indoor_temp
            else:
                _smoothed_indoor_temp += TEMP_SMOOTHING_ALPHA * (indoor_temp - _smoothed_indoor_temp)

            if not state["weather_available"]:
                state["outdoor_temperature"] = indoor_temp

            now = datetime.now(UTC)
            raw_occupied = state["occupant_count"] > 0

            if raw_occupied:
                _last_occupied_at = now
                occupied = True
            elif _last_occupied_at is not None and (now - _last_occupied_at) < timedelta(seconds=OCCUPANCY_GRACE_SECONDS):
                # Sensor just said 0, but the room was occupied within the
                # grace window -- hold steady instead of flickering.
                occupied = True
            else:
                occupied = False

            # Override Auto-revert logic
            for dev_name, ov in state["overrides"].items():
                if not ov["active"]:
                    continue

                on_now = _device_on(dev_name)

                if occupied and not on_now:
                    if ov["expires_at"] is not None:
                        ov["expires_at"] = None

                elif not occupied and on_now:
                    if ov["expires_at"] is None:
                        minutes = ov.get("duration_minutes") or 30
                        ov["expires_at"] = (now + timedelta(minutes=minutes)).isoformat()

                if ov["expires_at"] is not None and now >= datetime.fromisoformat(ov["expires_at"]):
                    ov["active"] = False
                    ov["expires_at"] = None
                    ov["duration_minutes"] = None

            # LIGHT 1 logic
            if not state["overrides"]["light_1"]["active"]:
                new_light_state = occupied
                if new_light_state != state["light_1_on"]:
                    state["light_1_on"] = new_light_state
                    set_relay("light_1", new_light_state)

            # LIGHT 2 logic
            if not state["overrides"]["light_2"]["active"]:
                new_light_state = occupied
                if new_light_state != state["light_2_on"]:
                    state["light_2_on"] = new_light_state
                    set_relay("light_2", new_light_state)

            # FAN logic (Now triggered right after lights and directly follows occupancy state)
            if not state["overrides"]["fan"]["active"]:
                new_fan_state = occupied
                if new_fan_state != state["fan_on"]:
                    state["fan_on"] = new_fan_state
                    set_relay("fan", new_fan_state)

            # Update savings ROI
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
        # No expiry is set here on purpose. Manual overrides (on OR off) hold
        # indefinitely until the user flips the device again or hits "Auto".
        # The control loop is the only place that ever starts a countdown,
        # and it only does that for a forced-ON device once the room goes
        # unoccupied (an energy-saving auto-revert) — never for forced-OFF.
        state["overrides"][device] = {
            "active": True,
            "expires_at": None,
            "duration_minutes": duration,
        }

        if device == "fan":
            state["fan_on"] = desired_state
        elif device == "light_1":
            state["light_1_on"] = desired_state
        elif device == "light_2":
            state["light_2_on"] = desired_state

        set_relay(device, desired_state)

    return jsonify({"ok": True, "state": state})


@app.route("/api/override/cancel", methods=["POST"])
def cancel_override():
    data = request.get_json(silent=True)
    device = data.get("device") if data else None
    if device not in RELAY_PINS:
        return jsonify({"ok": False, "error": f"device must be one of {list(RELAY_PINS)}"}), 400

    with state_lock:
        state["overrides"][device] = {"active": False, "expires_at": None, "duration_minutes": None}
    return jsonify({"ok": True, "state": state})


@app.route("/api/savings/reset", methods=["POST"])
def savings_reset():
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
        print("[WARNING] Flask debug mode is ON")
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    app.run(debug=DEBUG_MODE, host="0.0.0.0", port=5000)
