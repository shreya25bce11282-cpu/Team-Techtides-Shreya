import cv2
import requests
import time
from ultralytics import YOLO

# This must match the port your Flask app is running on
API_URL = "http://127.0.0.1:5000/api/sensor-update"

def main():
    print("Loading YOLOv8 Nano model...")
    model = YOLO("yolov8n.pt") 
    
    print("Waking up the Pi Camera...")
    cap = cv2.VideoCapture(0) 
    
    if not cap.isOpened():
        print("CRITICAL ERROR: Could not open the camera.")
        return

    print("Camera active. Entering Sleep/Wake monitor mode...")
    
    last_post_time = 0
    post_interval = 2.0 

    # --- WAKE/SLEEP STATE VARIABLES ---
    is_awake = False
    sleep_timer_start = time.time()
    prev_frame_gray = None
    
    # How many seconds of ZERO people before we put YOLO back to sleep?
    YOLO_SLEEP_DELAY = 15.0 
    
    while True:
        ret, frame = cap.read()
        if not ret:
            print("Warning: Dropped a frame, retrying...")
            time.sleep(0.5)
            continue

        # 1. PREPARE FRAME FOR CHEAP MOTION DETECTION
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if prev_frame_gray is None:
            prev_frame_gray = gray
            continue

        # 2. SOFTWARE "RADAR" (Frame Differencing)
        # This takes almost 0 CPU compared to YOLO
        diff = cv2.absdiff(prev_frame_gray, gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        motion_score = cv2.countNonZero(thresh)
        prev_frame_gray = gray

        occupant_count = 0
        current_time = time.time()

        # 3. WAKE/SLEEP LOGIC
        if not is_awake:
            # If a large enough block of pixels changes, trigger a wake up!
            # You can raise or lower '3000' to change sensitivity
            if motion_score > 3000:  
                print(f"\n[MOTION DETECTED] Score: {motion_score}. Waking up YOLO...")
                is_awake = True
                sleep_timer_start = current_time
        else:
            # We are AWAKE. Run the heavy AI model.
            results = model(frame, classes=[0], verbose=False)
            occupant_count = len(results[0].boxes)

            if occupant_count > 0:
                # Reset the sleep timer as long as someone is in the room
                sleep_timer_start = current_time
            elif (current_time - sleep_timer_start) > YOLO_SLEEP_DELAY:
                # Room has been empty too long. Go back to sleep.
                print("\n[ROOM EMPTY] No occupants for 15s. Putting YOLO to sleep...")
                is_awake = False
                
        # 4. POST TO DASHBOARD 
        if current_time - last_post_time >= post_interval:
            # Send updates whether asleep or awake so the dashboard always 
            # reflects the correct live state.
            payload = {"occupant_count": occupant_count}
            try:
                res = requests.post(API_URL, json=payload, timeout=2)
                if res.status_code == 200 and is_awake:
                    print(f"--> Awake: Sent {occupant_count} occupants to dashboard")
            except Exception:
                pass 
            
            last_post_time = current_time

    # Cleanup when finished
    cap.release()

if __name__ == "__main__":
    main()
