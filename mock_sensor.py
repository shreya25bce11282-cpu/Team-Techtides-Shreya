"""
Mock Occupancy Feeder
=======================
Your radar/camera teammates are building the real occupant-counting
pipeline. Until that's ready (or if you just want to test without
hardware), run this script alongside app.py -- it POSTs fake but
realistic occupant counts to your backend every few seconds, exactly the
way the real radar+camera script eventually will.

Temperature and humidity are NOT sent from here anymore -- app.py pulls
those from the weather API on its own, automatically.

Run this in a SECOND terminal, while app.py is running in the first:
    python mock_sensor.py

Try editing SCENARIO below to test different situations: an empty room,
a packed classroom, people trickling in, etc.
"""

import random
import time
import requests

API_URL = "http://127.0.0.1:5000/api/sensor-update"

# Change this to test different situations manually, or leave as "random"
SCENARIO = "random"  # "random" | "empty" | "packed" | "trickle_in"

# state used by the "trickle_in" scenario so occupancy climbs gradually
_trickle_count = 0


def next_reading():
    global _trickle_count

    if SCENARIO == "empty":
        return {"occupant_count": 0}

    if SCENARIO == "packed":
        return {"occupant_count": random.randint(25, 40)}

    if SCENARIO == "trickle_in":
        _trickle_count += 1
        if _trickle_count > 30:
            _trickle_count = 0  # loop back to empty
        return {"occupant_count": _trickle_count}

    # default: fully random, good for general testing
    return {"occupant_count": random.randint(0, 15)}


if __name__ == "__main__":
    print(f"Mock occupancy feed -> {API_URL} | scenario = {SCENARIO}")
    while True:
        reading = next_reading()
        try:
            requests.post(API_URL, json=reading, timeout=2)
            print(f"sent -> {reading}")
        except requests.exceptions.ConnectionError:
            print("Couldn't reach app.py -- is it running?")
        time.sleep(3)