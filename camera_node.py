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

    print("Camera active. Beginning detection loop...")
    print(">>> Press 'q' in the video window to quit <<<")
    
    last_post_time = 0
    post_interval = 2.0 

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Warning: Dropped a frame, retrying...")
            time.sleep(0.5)
            continue

        # Run inference for 'person' (class 0)
        results = model(frame, classes=[0], verbose=False)
        occupant_count = len(results[0].boxes)

        # ----- NEW VISUALIZATION CODE -----
        # This draws the bounding boxes and labels onto the image
        annotated_frame = results[0].plot()
        
        # This opens a window showing the live feed (only works on a desktop interface)
        cv2.imshow("AEGIS-Eco AI Vision", annotated_frame)
        # ----------------------------------

        current_time = time.time()
        if current_time - last_post_time >= post_interval:
            payload = {"occupant_count": occupant_count}
            try:
                res = requests.post(API_URL, json=payload, timeout=2)
                if res.status_code == 200:
                    print(f"--> Success: Sent {occupant_count} occupants to dashboard")
            except Exception:
                pass # Silently fail if dashboard isn't running to keep video smooth
            
            last_post_time = current_time

        # Listen for the 'q' key to be pressed to close the script safely
        if cv2.waitKey(1) & 0xFF == ord('q'):
            print("Closing camera feed...")
            break
            
    # Cleanup when finished
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
