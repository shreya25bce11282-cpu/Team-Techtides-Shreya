#!/bin/bash

# Tell the script which display to use for graphical applications
export DISPLAY=:0

# Navigate to your project folder
cd /home/raspberrypi/smart_room

# Activate the YOLO virtual environment
source /home/raspberrypi/yolo_object/bin/activate

# 1. Start the Flask Backend in the background
python app.py > backend_error.log 2>&1 &

# 2. Start the Camera Node in the background (detached input to prevent segfault)
libcamerify python camera_node.py < /dev/null > camera_error.log 2>&1 &

# 3. Wait 10 seconds for the servers to spin up
sleep 10

# 4. Launch Chromium in Kiosk mode
chromium --incognito --kiosk --noerrdialogs --disable-infobars --hide-scrollbars --password-store=basic http://127.0.0.1:5000
