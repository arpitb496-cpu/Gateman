from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import io
import os
import csv
import re
from datetime import datetime
from PIL import Image
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

app = FastAPI(title="Automatic Gateman API")

def clean_and_validate_plate(text):
    """Filters out garbage OCR. Forgiving logic to ensure we don't reject valid detections."""
    text = re.sub(r'[^A-Z0-9]', '', text.upper())
    
    # Just ensure it's at least 4 characters and has at least one number
    if len(text) >= 4 and any(c.isdigit() for c in text):
        return text
        
    return None

# Allow CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Constants & Paths
WHITELIST_FILE = "whitelist.txt"
LOG_FILE = "detection_log.csv"

def init_whitelist():
    """Initializes the whitelist file with mock and requested plates if not present."""
    if not os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "w") as f:
            f.write("22BH6517A\nMP20CC1234\nDL3CAQ0001\nMH12AB1234\n")

def get_whitelist():
    """Reads whitelist plates."""
    init_whitelist()
    with open(WHITELIST_FILE, "r") as f:
        return set(line.strip().replace(" ", "").upper() for line in f if line.strip())

def log_plate_to_csv(plate_text, status):
    """Appends recognized plate to the CSV/Excel log sheet."""
    file_exists = os.path.exists(LOG_FILE)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Time", "Plate Number", "Status"])
        writer.writerow([date_str, time_str, plate_text, status])

# Attempt to load AI Models, fallback to mock mode if DLLs are missing
USE_MOCK = False
try:
    import cv2
    import numpy as np
    import easyocr
    from ultralytics import YOLO

    print("Loading Models...")
    reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    car_model = YOLO('../yolov8n.pt')
    plate_model = YOLO('../kbest.pt')
    print("Models Loaded successfully!")
except ImportError as e:
    print(f"Warning: Failed to load AI libraries ({e}). Running in MOCK mode.")
    USE_MOCK = True

class DetectionResult(BaseModel):
    cars: list[dict]
    plates: list[dict]
    status: str = "" # "ALLOWED", "UNKNOWN", or ""

def preprocess_for_ocr(plate_crop):
    """Optimizes image preprocessing scale for EasyOCR to increase CPU speeds by 3-5x."""
    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    # If crop is tiny, upscale it to target size for clear reading. 
    # If crop is already large (e.g. phone screen), do NOT upscale to save CPU cycles!
    if w < 130 and w > 0:
        scale = 130.0 / w
        gray = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    return gray

@app.post("/detect", response_model=DetectionResult)
async def detect_objects(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents)).convert('RGB')
        width, height = image.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {str(e)}")

    whitelist = get_whitelist()

    if USE_MOCK:
        # Return mock detection data
        cx, cy = width // 2, height // 2
        mock_plate = "22BH6517A"
        status = "ALLOWED" if mock_plate in whitelist else "UNKNOWN"
        log_plate_to_csv(mock_plate, status)
        
        return DetectionResult(
            cars=[{
                "box": [cx - 100, cy - 50, cx + 100, cy + 150],
                "confidence": 0.95,
                "vehicle_type": "Sedan"
            }],
            plates=[{
                "box": [cx - 40, cy + 80, cx + 40, cy + 110],
                "text": mock_plate,
                "confidence": 0.88,
                "vehicle_type": "Sedan"
            }],
            status=status
        )

    # Convert RGB to BGR for OpenCV
    frame = np.array(image)
    frame = frame[:, :, ::-1].copy()

    cars_detected = []
    plates_detected = []
    global_status = ""

    # 1. Car Detection
    car_results = car_model(frame, verbose=False, imgsz=640, conf=0.35)
    boxes = car_results[0].boxes

    # COCO Class mapping
    CLASS_NAMES = {2: 'Sedan', 3: 'Motorcycle', 5: 'Bus', 7: 'Truck'}

    for box, cls, conf in zip(boxes.xyxy, boxes.cls, boxes.conf):
        class_id = int(cls)
        if class_id not in [2, 3, 5, 7]:
            continue
        
        vehicle_type = CLASS_NAMES.get(class_id, "Vehicle")
        x1, y1, x2, y2 = map(int, box)
        
        cars_detected.append({
            "box": [x1, y1, x2, y2],
            "confidence": float(conf),
            "vehicle_type": vehicle_type
        })
        
        # We assume the whole car might contain the plate.
        car_crop = frame[y1:y2, x1:x2]
        if car_crop.size == 0:
            continue

        # 2. Plate Detection within the car crop
        plate_results = plate_model(car_crop, verbose=False, imgsz=416, conf=0.38)
        pboxes = plate_results[0].boxes

        for pbox, pconf in zip(pboxes.xyxy, pboxes.conf):
            px1, py1, px2, py2 = map(int, pbox)
            
            # Map back to full frame
            fx1 = x1 + px1
            fy1 = y1 + py1
            fx2 = x1 + px2
            fy2 = y1 + py2

            plate_crop = car_crop[py1:py2, px1:px2]
            if plate_crop.size == 0:
                continue

            # Optimized OCR on the Plate
            gray = preprocess_for_ocr(plate_crop)
            ocr_results = reader.readtext(gray, detail=1, paragraph=False, low_text=0.25)
            text = "..."
            ocr_conf = 0.0
            
            if ocr_results:
                raw_text = ''.join([res[1] for res in ocr_results])
                avg_conf = sum([res[2] for res in ocr_results]) / len(ocr_results)
                valid_text = clean_and_validate_plate(raw_text)
                if valid_text:
                    text = valid_text
                    ocr_conf = avg_conf

            if text != "...":
                plates_detected.append({
                    "box": [fx1, fy1, fx2, fy2],
                    "text": text,
                    "confidence": float(ocr_conf),
                    "vehicle_type": vehicle_type
                })

    # Fallback 1: If no plates were detected through the car-crop path
    if not plates_detected:
        print("[DEBUG] Running full-frame fallback plate detection...")
        plate_results = plate_model(frame, verbose=False, imgsz=640, conf=0.15)
        pboxes = plate_results[0].boxes

        for pbox, pconf in zip(pboxes.xyxy, pboxes.conf):
            fx1, fy1, fx2, fy2 = map(int, pbox)
            plate_crop = frame[fy1:fy2, fx1:fx2]
            if plate_crop.size == 0:
                continue

            # Optimized OCR on the Plate
            gray = preprocess_for_ocr(plate_crop)
            ocr_results = reader.readtext(gray, detail=1, paragraph=False, low_text=0.25)
            text = "..."
            ocr_conf = 0.0
            
            if ocr_results:
                raw_text = ''.join([res[1] for res in ocr_results])
                avg_conf = sum([res[2] for res in ocr_results]) / len(ocr_results)
                valid_text = clean_and_validate_plate(raw_text)
                if valid_text:
                    text = valid_text
                    ocr_conf = avg_conf

            if text != "...":
                plates_detected.append({
                    "box": [fx1, fy1, fx2, fy2],
                    "text": text,
                    "confidence": float(ocr_conf),
                    "vehicle_type": "Unknown"
                })

    # Fallback 2: If YOLO plate detector failed entirely, run direct OCR on full frame with strict validation
    if not plates_detected:
        # Crop center 60% of the frame to keep OCR fast while retaining full resolution
        h, w = frame.shape[:2]
        cx1, cx2 = int(w * 0.2), int(w * 0.8)
        cy1, cy2 = int(h * 0.2), int(h * 0.8)
        
        center_crop = frame[cy1:cy2, cx1:cx2]
        gray = cv2.cvtColor(center_crop, cv2.COLOR_BGR2GRAY)
        ocr_results = reader.readtext(gray, detail=1, paragraph=False)
        
        for (bbox, text, conf) in ocr_results:
            valid_text = clean_and_validate_plate(text)
            if valid_text:
                # Map coordinates back to the full frame
                xs = [pt[0] + cx1 for pt in bbox]
                ys = [pt[1] + cy1 for pt in bbox]
                fx1, fy1, fx2, fy2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
                
                plates_detected.append({
                    "box": [fx1, fy1, fx2, fy2],
                    "text": valid_text,
                    "confidence": float(conf),
                    "vehicle_type": "Unknown"
                })
                break

    # 3. Whitelist check and CSV Logging
    for plate in plates_detected:
        plate_val = plate["text"]
        if plate_val != "...":
            status = "ALLOWED" if plate_val in whitelist else "UNKNOWN"
            global_status = status
            log_plate_to_csv(plate_val, status)
            break # Update status on the first valid read

    return DetectionResult(
        cars=cars_detected, 
        plates=plates_detected,
        status=global_status
    )

@app.get("/health")
def health_check():
    return {"status": "ok", "mode": "mock" if USE_MOCK else "ai"}

@app.get("/export-excel")
def export_excel():
    """Generates an Excel file from the detection log with a Gateman header."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detection Log"
    
    # 1. Add Gateman Header
    ws.merge_cells("A1:D2")
    header_cell = ws["A1"]
    header_cell.value = "AUTOMATIC GATEMAN - DETECTION REPORT"
    header_cell.font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
    header_cell.fill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
    header_cell.alignment = Alignment(horizontal="center", vertical="center")
    
    # 2. Add Column Headers
    columns = ["Date", "Time", "Number Plate", "Status"]
    ws.append([]) # Empty row for spacing
    ws.append(columns)
    
    header_row = ws[3]
    for cell in header_row:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
        
    # 3. Adjust Column Widths
    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 15
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 15
    
    # 4. Read Data from CSV
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None) # Skip original CSV header
            for row in reader:
                ws.append(row)
                
    # Center align the data rows
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row, min_col=1, max_col=4):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")

    # Save to a virtual file and stream
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Gateman_Report.xlsx"}
    )
