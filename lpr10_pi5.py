import cv2
import easyocr
import os
from ultralytics import YOLO
import threading
import time
import logging
import numpy as np
from datetime import datetime
from collections import defaultdict

# === Setup ===
output_car_dir   = "output/cars"
output_plate_dir = "output/plates"
os.makedirs(output_car_dir,   exist_ok=True)
os.makedirs(output_plate_dir, exist_ok=True)

# === Logging (only detections & errors) ===
logging.basicConfig(
    filename="detection_log.txt",
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

def log(msg):
    logging.info(msg)
    # print(msg)   # uncomment for debug

# === Models ===
reader = easyocr.Reader(['en'], gpu=False, verbose=False)
car_model   = YOLO('yolov8n.pt')
plate_model = YOLO('kbest.pt')

# === Webcam (optimized for speed) ===
cap = cv2.VideoCapture(0, cv2.CAP_ANY)

if not cap.isOpened():
    log("Cannot open camera index 0 → trying 1")
    cap = cv2.VideoCapture(1)
    if not cap.isOpened():
        log("Cannot open camera at all")
        exit(1)

cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
cap.set(cv2.CAP_PROP_FPS, 30)

log("Camera opened")

# Colors & font
COLOR_CAR   = (255, 140, 0)   # orange-blue
COLOR_PLATE = (0, 255, 0)     # green
FONT = cv2.FONT_HERSHEY_SIMPLEX

# Store latest OCR results: key = (x1,y1) approx position → value = text
ocr_results = defaultdict(str)   # position → detected text

prev_time = time.time()

# === OCR thread – puts result into shared dict ===
def ocr_thread(plate_crop, plate_pos_key):
    try:
        gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
        # Fast resize + light processing
        gray = cv2.resize(gray, (0,0), fx=1.8, fy=1.8, interpolation=cv2.INTER_LINEAR)
        gray = cv2.GaussianBlur(gray, (3,3), 0)

        results = reader.readtext(gray, detail=0, paragraph=False, low_text=0.25)
        if results:
            text = ''.join(results).replace(" ", "").upper()
            if len(text) >= 5:
                ocr_results[plate_pos_key] = text
                log(f"Plate detected: {text} at approx pos {plate_pos_key}")
    except:
        pass

# === Main real-time loop ===
while True:
    ret, frame = cap.read()
    if not ret:
        time.sleep(0.05)
        continue

    # Car detection
    car_results = car_model(frame, verbose=False, imgsz=640, conf=0.35)
    boxes = car_results[0].boxes

    current_vehicles = 0

    for box, cls, conf in zip(boxes.xyxy, boxes.cls, boxes.conf):
        if int(cls) != 2:  # only cars
            continue
        if conf < 0.38:
            continue

        current_vehicles += 1

        x1, y1, x2, y2 = map(int, box)

        # Draw car bounding box
        cv2.rectangle(frame, (x1, y1), (x2, y2), COLOR_CAR, 2)
        cv2.putText(frame, f"CAR {conf:.2f}", (x1, y1-10),
                    FONT, 0.65, COLOR_CAR, 2)

        car_crop = frame[y1:y2, x1:x2]
        if car_crop.size == 0:
            continue

        # Plate detection (small crop = fast)
        plate_results = plate_model(car_crop, verbose=False, imgsz=416, conf=0.38)
        pboxes = plate_results[0].boxes

        for pbox in pboxes.xyxy:
            px1, py1, px2, py2 = map(int, pbox)

            # Map back to full frame
            fx1 = x1 + px1
            fy1 = y1 + py1
            fx2 = x1 + px2
            fy2 = y1 + py2

            plate_crop = car_crop[py1:py2, px1:px2]
            if plate_crop.size == 0:
                continue

            # Unique-ish key for this plate (top-left corner rounded)
            pos_key = (fx1 // 10 * 10, fy1 // 10 * 10)

            # Draw plate rectangle
            cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), COLOR_PLATE, 2)

            # Show OCR result if available, otherwise "..." 
            display_text = ocr_results.get(pos_key, "...")
            text_color = (0, 255, 255) if display_text == "..." else (255, 255, 0)
            cv2.putText(frame, display_text, (fx1, fy1 - 5),
                        FONT, 0.58, text_color, 1, cv2.LINE_AA)

            # Start OCR if not already processing this position recently
            # (simple dedup – you can improve with timestamp check later)
            if display_text == "...":
                threading.Thread(
                    target=ocr_thread,
                    args=(plate_crop.copy(), pos_key),
                    daemon=True
                ).start()

    # FPS & status overlay
    now = time.time()
    fps = 1 / (now - prev_time) if (now - prev_time) > 0 else 0
    prev_time = now

    cv2.putText(frame, f"FPS: {fps:.1f}", (8, 25),
                FONT, 0.7, (0, 255, 255), 2)

    if current_vehicles > 0:
        cv2.putText(frame, f"{current_vehicles} VEHICLE{'S' if current_vehicles > 1 else ''}",
                    (8, 55), FONT, 0.8, (0, 0, 255), 2)

    cv2.imshow("Real-time Number Plate OCR", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# Cleanup
cap.release()
cv2.destroyAllWindows()
log("Program stopped")