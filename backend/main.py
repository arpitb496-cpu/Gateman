import os
import io
import csv
import re
import time
import threading
from datetime import datetime
from PIL import Image

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Side, Border

# Supabase optional integration
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Connected to Supabase Cloud Database!")
    except Exception as e:
        print(f"Supabase connection notice: {e}")

# Base directories and resource paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR) if os.path.basename(BASE_DIR) == "backend" else BASE_DIR

def find_file(filename):
    """Locate file in project root or current dir."""
    p_root = os.path.join(PROJECT_ROOT, filename)
    if os.path.exists(p_root):
        return p_root
    p_base = os.path.join(BASE_DIR, filename)
    if os.path.exists(p_base):
        return p_base
    return p_root

WHITELIST_FILE = find_file("whitelist.txt")
LOG_FILE = find_file("detection_log.csv")
CAR_MODEL_PATH = find_file("yolov8s.pt") if os.path.exists(find_file("yolov8s.pt")) else find_file("yolov8n.pt")
PLATE_MODEL_PATH = find_file("kbest.pt")

app = FastAPI(title="Automatic Gateman API - Apple Silicon GPU Accelerated")

# CORS setup (W3C compliant)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def init_whitelist():
    """Initializes the whitelist file with default allowed plates if not present."""
    if not os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            f.write("22BH6517A\nMP20CC1234\nDL3CAQ0001\nMH12AB1234\nMH20DV2366\n")

def get_whitelist():
    """Reads whitelisted plate numbers."""
    init_whitelist()
    plates = set()
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                p = re.sub(r'[^A-Z0-9]', '', line.strip().upper())
                if p:
                    plates.add(p)
    except Exception as e:
        print(f"Error reading whitelist: {e}")
    return plates

_last_logged_plates = {}
_csv_lock = threading.Lock()

def log_plate_to_db(plate_text, status):
    """Logs recognized plate to Supabase or local CSV fallback with cooldown and thread-lock to prevent race conditions."""
    now = datetime.now()
    now_ts = time.time()

    # Prune old cooldown entries (>60s) to keep dictionary lean
    if len(_last_logged_plates) > 200:
        for k in list(_last_logged_plates.keys()):
            if (now_ts - _last_logged_plates[k]) > 60.0:
                _last_logged_plates.pop(k, None)

    # 15-second cooldown for the same plate standing in front of the gate camera
    if plate_text in _last_logged_plates and (now_ts - _last_logged_plates[plate_text]) < 15.0:
        return
    _last_logged_plates[plate_text] = now_ts

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    if supabase:
        try:
            supabase.table("detection_log").insert({
                "date": date_str,
                "time": time_str,
                "plate_number": plate_text,
                "status": status
            }).execute()
            return
        except Exception as e:
            print(f"Supabase logging error: {e}")

    with _csv_lock:
        file_exists = os.path.exists(LOG_FILE)
        try:
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists or os.path.getsize(LOG_FILE) == 0:
                    writer.writerow(["Date", "Time", "Plate Number", "Status"])
                writer.writerow([date_str, time_str, plate_text, status])
        except Exception as e:
            print(f"CSV log error: {e}")

# Hardware Acceleration Setup: Apple Silicon Metal Performance Shaders (MPS)
import torch
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
USE_GPU = (DEVICE == "mps")

# Load AI Models
USE_MOCK = False
try:
    import cv2
    import numpy as np
    import easyocr
    from ultralytics import YOLO

    device_name = "Apple Silicon Metal (MPS) GPU" if USE_GPU else "Multi-core CPU"
    print(f"🚀 Initializing AI Models on {device_name}...")
    reader = easyocr.Reader(['en'], gpu=USE_GPU, verbose=False)
    car_model = YOLO(CAR_MODEL_PATH)
    plate_model = YOLO(PLATE_MODEL_PATH)
    
    # Warm-up models on GPU for zero-latency live inference
    warmup_img = np.zeros((320, 320, 3), dtype=np.uint8)
    car_model.predict(warmup_img, device=DEVICE, verbose=False)
    plate_model.predict(warmup_img, device=DEVICE, verbose=False)
    reader.readtext(warmup_img[100:200, 100:200])

    print(f"✅ Active: Vehicle Model [{os.path.basename(CAR_MODEL_PATH)}] & Plate Model [{os.path.basename(PLATE_MODEL_PATH)}] on {DEVICE.upper()} GPU!")
except Exception as e:
    print(f"⚠️ Warning: Could not initialize AI models ({e}). Falling back to mock mode.")
    USE_MOCK = True

INDIAN_STATES = {
    'AP', 'AR', 'AS', 'BR', 'CG', 'GA', 'GJ', 'HR', 'HP', 'JH', 'KA', 'KL',
    'MP', 'MH', 'MN', 'ML', 'MZ', 'NL', 'OD', 'OR', 'PB', 'RJ', 'SK', 'TN', 'TS',
    'TR', 'UP', 'UK', 'UA', 'WB', 'AN', 'CH', 'DN', 'DD', 'DL', 'JK', 'LA', 'LD', 'PY'
}

COMMON_PREFIX_WORDS = ['HARYANA', 'MAHARASHTRA', 'DELHI', 'PUNJAB', 'GUJARAT', 'RAJASTHAN', 'KARNATAKA', 'KERALA', 'TAMILNADU', 'UTTARPRADESH', 'INDIA']
CHAR_TO_NUM = {'O': '0', 'Q': '0', 'I': '1', 'L': '1', 'Z': '2', 'B': '8', 'S': '5', 'G': '6', 'T': '1'}
NUM_TO_CHAR = {'0': 'O', '1': 'I', '2': 'Z', '8': 'B', '5': 'S', '6': 'G'}

def get_levenshtein(s1, s2):
    """Calculates Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return get_levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def clean_and_validate_plate(text, whitelist=None):
    """
    High-Precision Indian License Plate Validator & Auto-Correction:
    - Filters out noise, labels, posters, and short random text.
    - Strips state header words (HARYANA, MAHARASHTRA, DELHI, IND).
    - Normalizes positional character confusions (e.g. MP2O -> MP20, Z in tail -> 2).
    - Strictly enforces Indian RTO syntax: [2 letters State] [1-2 digits RTO] [1-3 letters Series] [3-4 digits Number].
    - Fuzzy matches (1 edit distance) against the whitelist to prevent gate lockouts from minor OCR glare.
    """
    raw = re.sub(r'[^A-Z0-9]', '', text.upper())
    
    # 0. Direct whitelist match
    if whitelist and raw in whitelist:
        return raw

    # 1. Fuzzy whitelist match (if 1 character typo away from an authorized plate)
    if whitelist:
        for wl_plate in whitelist:
            if get_levenshtein(raw, wl_plate) <= 1:
                return wl_plate

    # Strip printed state name headers if present
    for word in COMMON_PREFIX_WORDS:
        if raw.startswith(word) and len(raw) >= len(word) + 7:
            raw = raw[len(word):]
            break

    # Strip leading 'IND' stamp if attached
    if raw.startswith("IND") and len(raw) >= 9:
        raw = raw[3:]

    # Plates must be between 7 and 11 characters
    if len(raw) < 7 or len(raw) > 11:
        return None

    # Positional character grammar normalization
    chars = list(raw)
    n = len(chars)

    # Positions 0 & 1: ALWAYS State letters (e.g. 0L -> DL, 1R -> HR)
    chars[0] = NUM_TO_CHAR.get(chars[0], chars[0])
    chars[1] = NUM_TO_CHAR.get(chars[1], chars[1])

    # Position 2: ALWAYS District first digit (e.g. MPO -> MP0, DL3)
    chars[2] = CHAR_TO_NUM.get(chars[2], chars[2])

    # Position 3: If followed by series letters, Position 3 is also District digit (e.g. MP2O -> MP20, MH2O -> MH20)
    if n >= 9 and chars[3] in ['O', 'Q', 'D', 'I', 'Z', 'B', 'S']:
        chars[3] = CHAR_TO_NUM.get(chars[3], chars[3])

    # Last 3 to 4 characters: ALWAYS Numbers (e.g. Z116 -> 2116, O -> 0, I -> 1)
    tail_len = 4 if n >= 9 else 3
    for i in range(n - tail_len, n):
        chars[i] = CHAR_TO_NUM.get(chars[i], chars[i])

    fixed_text = ''.join(chars)

    # Fuzzy whitelist re-check after character normalization
    if whitelist:
        for wl_plate in whitelist:
            if get_levenshtein(fixed_text, wl_plate) <= 1:
                return wl_plate

    # 1. Standard Indian Plate Match:
    # State (2 letters) + RTO (1-2 digits) + Series (1-3 letters) + Number (3-4 digits)
    m = re.match(r'^([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{3,4})$', fixed_text)
    if m and m.group(1) in INDIAN_STATES:
        return fixed_text

    # 2. BH Series Plate Match:
    # Year (2 digits) + BH + Number (4 digits) + Series (1-2 letters)
    bh = re.match(r'^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$', fixed_text)
    if bh:
        return fixed_text

    return None

def cluster_ocr_lines(ocr_res):
    """
    Robust line clustering for multi-line and single-line license plates.
    Prevents vertical jitter from swapping adjacent characters.
    """
    if not ocr_res:
        return []
    items = []
    for item in ocr_res:
        pts = item[0]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
        cy = (y1 + y2) / 2.0
        h = max(1, y2 - y1)
        items.append({"cy": cy, "x1": x1, "h": h, "raw": item})
    
    items.sort(key=lambda it: it["cy"])
    avg_h = sum(it["h"] for it in items) / len(items)
    
    lines = []
    current_line = []
    current_cy = None
    
    for it in items:
        if current_cy is None:
            current_line.append(it)
            current_cy = it["cy"]
        elif abs(it["cy"] - current_cy) < avg_h * 0.55:
            current_line.append(it)
            current_cy = sum(x["cy"] for x in current_line) / len(current_line)
        else:
            lines.append(current_line)
            current_line = [it]
            current_cy = it["cy"]
    if current_line:
        lines.append(current_line)
        
    ordered = []
    for line in lines:
        line.sort(key=lambda it: it["x1"])
        for it in line:
            ordered.append(it["raw"])
    return ordered

def preprocess_plate_crop(crop):
    """Enhances plate crop for high-accuracy OCR."""
    h, w = crop.shape[:2]
    if h == 0 or w == 0:
        return crop

    # Target height between 80px and 140px for optimal EasyOCR CRAFT detection
    target_h = 90
    if h < target_h or w < 220:
        scale = max(target_h / h, 220.0 / w)
        crop = cv2.resize(crop, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # Grayscale + CLAHE for high contrast
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    return gray

class DetectionResult(BaseModel):
    cars: list[dict]
    plates: list[dict]
    status: str = ""

@app.post("/detect", response_model=DetectionResult)
def detect_objects(file: UploadFile = File(...)):
    try:
        contents = file.file.read()
        image = Image.open(io.BytesIO(contents)).convert('RGB')
        width, height = image.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {str(e)}")

    whitelist = get_whitelist()

    if USE_MOCK:
        cx, cy = width // 2, height // 2
        mock_plate = "22BH6517A"
        status = "ALLOWED" if mock_plate in whitelist else "UNKNOWN"
        log_plate_to_db(mock_plate, status)
        return DetectionResult(
            cars=[{"box": [cx - 120, cy - 60, cx + 120, cy + 140], "confidence": 0.95, "vehicle_type": "Sedan"}],
            plates=[{"box": [cx - 60, cy + 50, cx + 60, cy + 90], "text": mock_plate, "confidence": 0.92, "vehicle_type": "Sedan"}],
            status=status
        )

    # Convert RGB PIL to BGR OpenCV
    frame = np.array(image)[:, :, ::-1].copy()

    cars_detected = []
    plates_detected = []
    global_status = ""

    # 1. Detect License Plates on Apple Silicon GPU (native 640px model resolution, conf=0.25 for high sensitivity)
    plate_results = plate_model.predict(frame, verbose=False, imgsz=640, conf=0.25, device=DEVICE)
    pboxes = plate_results[0].boxes

    candidate_boxes = []
    for pbox, pconf in zip(pboxes.xyxy, pboxes.conf):
        px1, py1, px2, py2 = map(int, pbox)
        bw = px2 - px1
        bh = py2 - py1
        # Geometry filter: License plates are horizontal rectangles
        if bw < 35 or bh < 10:
            continue
        aspect = bw / float(bh)
        if aspect < 1.1 or aspect > 7.0:
            continue
        candidate_boxes.append((px1, py1, px2, py2, float(pconf)))

    # 2. Detect Vehicles on GPU (imgsz=480 for fast classification)
    car_results = car_model.predict(frame, verbose=False, imgsz=480, conf=0.28, device=DEVICE)
    cboxes = car_results[0].boxes
    CLASS_NAMES = {2: 'Sedan', 3: 'Motorcycle', 5: 'Bus', 7: 'Truck'}

    for box, cls, conf in zip(cboxes.xyxy, cboxes.cls, cboxes.conf):
        cid = int(cls)
        if cid in [2, 3, 5, 7]:
            x1, y1, x2, y2 = map(int, box)
            cars_detected.append({
                "box": [x1, y1, x2, y2],
                "confidence": round(float(conf), 2),
                "vehicle_type": CLASS_NAMES.get(cid, "Vehicle")
            })

    # If no full-frame plate found, inspect car crops
    if not candidate_boxes and cars_detected:
        for car in cars_detected:
            cx1, cy1, cx2, cy2 = car["box"]
            car_crop = frame[cy1:cy2, cx1:cx2]
            if car_crop.size > 0:
                crop_res = plate_model.predict(car_crop, verbose=False, imgsz=416, conf=0.25, device=DEVICE)
                for cb, cconf in zip(crop_res[0].boxes.xyxy, crop_res[0].boxes.conf):
                    cpx1, cpy1, cpx2, cpy2 = map(int, cb)
                    bw = cpx2 - cpx1
                    bh = cpy2 - cpy1
                    if bw < 35 or bh < 10:
                        continue
                    aspect = bw / float(bh)
                    if aspect < 1.1 or aspect > 7.0:
                        continue
                    candidate_boxes.append((cx1 + cpx1, cy1 + cpy1, cx1 + cpx2, cy1 + cpy2, float(cconf)))

    # Prioritize top 3 most confident candidate boxes
    candidate_boxes.sort(key=lambda b: b[4], reverse=True)
    candidate_boxes = candidate_boxes[:3]

    # 3. Process candidate plate boxes with OCR
    H, W = frame.shape[:2]
    for (px1, py1, px2, py2, pconf) in candidate_boxes:
        # Add 6% margin padding so edge digits are preserved
        pad_x = int((px2 - px1) * 0.06)
        pad_y = int((py2 - py1) * 0.08)
        qx1 = max(0, px1 - pad_x)
        qy1 = max(0, py1 - pad_y)
        qx2 = min(W, px2 + pad_x)
        qy2 = min(H, py2 + pad_y)

        plate_crop = frame[qy1:qy2, qx1:qx2]
        if plate_crop.size == 0:
            continue

        # Associate with enclosing car if present
        vtype = "Vehicle"
        for car in cars_detected:
            bx1, by1, bx2, by2 = car["box"]
            if bx1 <= px1 and by1 <= py1 and bx2 >= px2 and by2 >= py2:
                vtype = car["vehicle_type"]
                break

        # OCR Recognition
        proc_crop = preprocess_plate_crop(plate_crop)
        ocr_res = reader.readtext(proc_crop, detail=1, paragraph=False, low_text=0.2)

        recognized_text = "..."
        ocr_conf = 0.0

        if ocr_res:
            ocr_res = cluster_ocr_lines(ocr_res)
            raw_text = " ".join([r[1] for r in ocr_res])
            conf_sum = sum([r[2] for r in ocr_res]) / len(ocr_res)
            
            validated = clean_and_validate_plate(raw_text, whitelist)
            if validated and conf_sum >= 0.35:
                recognized_text = validated
                ocr_conf = conf_sum

        # Robust Fallback: If enhanced grayscale didn't produce a valid plate, try raw crop
        if recognized_text == "...":
            raw_ocr = reader.readtext(plate_crop, detail=0)
            if raw_ocr:
                val = clean_and_validate_plate("".join(raw_ocr), whitelist)
                if val:
                    recognized_text = val
                    ocr_conf = 0.70

        # Only register plate if recognized or if YOLO plate detector is confident (>0.40)
        if recognized_text != "..." or pconf >= 0.40:
            plates_detected.append({
                "box": [px1, py1, px2, py2],
                "text": recognized_text,
                "confidence": round(float(ocr_conf if recognized_text != "..." else pconf), 2),
                "vehicle_type": vtype
            })

    # 4. Check whitelist for status and log valid reads
    for plate in plates_detected:
        plate_str = plate["text"]
        if plate_str != "...":
            clean_str = re.sub(r'[^A-Z0-9]', '', plate_str.upper())
            status = "ALLOWED" if clean_str in whitelist else "UNKNOWN"
            global_status = status
            log_plate_to_db(clean_str, status)
            break

    return DetectionResult(
        cars=cars_detected,
        plates=plates_detected,
        status=global_status
    )

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "mode": "ai" if not USE_MOCK else "mock",
        "device": f"{DEVICE.upper()} (Apple Metal GPU)" if USE_GPU else "CPU",
        "platform": "Apple Silicon (macOS)",
        "models": {
            "vehicle": os.path.basename(CAR_MODEL_PATH),
            "plate": os.path.basename(PLATE_MODEL_PATH)
        }
    }

@app.get("/whitelist")
def get_whitelist_api():
    return {"whitelist": sorted(list(get_whitelist()))}

@app.post("/whitelist/add")
def add_to_whitelist(plate: str):
    clean = re.sub(r'[^A-Z0-9]', '', plate.upper())
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid plate number")
    wl = get_whitelist()
    if clean not in wl:
        wl.add(clean)
        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            for p in sorted(list(wl)):
                f.write(f"{p}\n")
    return {"status": "added", "plate": clean}

@app.post("/whitelist/remove")
def remove_from_whitelist(plate: str):
    clean = re.sub(r'[^A-Z0-9]', '', plate.upper())
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid plate number")
    wl = get_whitelist()
    if clean in wl:
        wl.remove(clean)
        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            for p in sorted(list(wl)):
                f.write(f"{p}\n")
        return {"status": "removed", "plate": clean}
    return {"status": "not_found", "plate": clean}

@app.get("/logs")
def get_logs(limit: int = 50):
    """Returns recent detection log entries."""
    rows = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                r = csv.reader(f)
                next(r, None)
                for line in r:
                    if len(line) >= 4:
                        rows.append({
                            "date": line[0],
                            "time": line[1],
                            "plate": line[2],
                            "status": line[3]
                        })
        except Exception as e:
            print(f"Log read error: {e}")
    # Return most recent first
    return {"logs": rows[-limit:][::-1]}

@app.post("/clear-logs")
def clear_logs():
    """Clears detection log CSV file and in-memory cooldown cache, resetting all counters to 0."""
    global _last_logged_plates
    with _csv_lock:
        try:
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Date", "Time", "Plate Number", "Status"])
            _last_logged_plates.clear()
            return {"status": "success", "message": "Detection logs cleared"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
def get_stats():
    """Returns aggregate dashboard metrics."""
    total_scans = 0
    unique_plates = set()
    allowed_count = 0
    unknown_count = 0

    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                r = csv.reader(f)
                next(r, None)
                for line in r:
                    if len(line) >= 4:
                        total_scans += 1
                        plate = line[2]
                        st = line[3]
                        unique_plates.add(plate)
                        if st == "ALLOWED":
                            allowed_count += 1
                        else:
                            unknown_count += 1
        except Exception as e:
            print(f"Stats read error: {e}")

    wl = get_whitelist()
    return {
        "total_scans": total_scans,
        "unique_plates": len(unique_plates),
        "allowed_count": allowed_count,
        "unknown_count": unknown_count,
        "whitelist_count": len(wl)
    }

@app.get("/export-excel")
def export_excel():
    """Generates a styled Excel report from detection logs."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detection Log"

    DARK_BLUE   = "1A365D"
    MID_BLUE    = "2B6CB0"
    LIGHT_BLUE  = "EBF4FF"
    ALT_ROW     = "F7FAFC"
    WHITE       = "FFFFFF"
    BORDER_CLR  = "CBD5E0"

    thin_side   = Side(style="thin",   color=BORDER_CLR)
    thick_side  = Side(style="medium", color=MID_BLUE)
    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    thick_border= Border(left=thick_side, right=thick_side, top=thick_side, bottom=thick_side)

    # Title Banner
    ws.merge_cells("A1:E2")
    title_cell = ws["A1"]
    title_cell.value = "AUTOMATIC GATEMAN - DETECTION REPORT"
    title_cell.font = Font(name="Calibri", size=16, bold=True, color=WHITE)
    title_cell.fill = PatternFill(start_color=DARK_BLUE, end_color=DARK_BLUE, fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 20

    # Subtitle with Timestamp
    ws.merge_cells("A3:E3")
    sub_cell = ws["A3"]
    sub_cell.value = f"Generated: {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
    sub_cell.font = Font(name="Calibri", size=10, italic=True, color=WHITE)
    sub_cell.fill = PatternFill(start_color=MID_BLUE, end_color=MID_BLUE, fill_type="solid")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 16

    # Headers
    headers = ["#", "Date", "Time", "Number Plate", "Status"]
    ws.append([])
    ws.append(headers)
    header_row_idx = 5
    col_widths = [6, 14, 14, 20, 16]

    for col_idx, (hdr, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=header_row_idx, column=col_idx)
        cell.value = hdr
        cell.font = Font(name="Calibri", size=11, bold=True, color=WHITE)
        cell.fill = PatternFill(start_color=MID_BLUE, end_color=MID_BLUE, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thick_border
        ws.column_dimensions[chr(64 + col_idx)].width = width
    ws.row_dimensions[header_row_idx].height = 20

    # Read data from Supabase or CSV
    rows = []
    if supabase:
        try:
            res = supabase.table("detection_log").select("*").execute()
            for r in res.data:
                rows.append((r.get("date", ""), r.get("time", ""), r.get("plate_number", ""), r.get("status", "ALLOWED")))
        except Exception as e:
            print(f"Supabase read error: {e}")

    if not rows and os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)
                for r in reader:
                    if len(r) >= 4:
                        rows.append((r[0], r[1], r[2], r[3]))
                    elif len(r) == 3:
                        rows.append((r[0], r[1], r[2], "ALLOWED"))
        except Exception as e:
            print(f"CSV read error: {e}")

    # Write Data
    for i, (date_val, time_val, plate_val, status_val) in enumerate(rows, start=1):
        row_idx = header_row_idx + i
        fill_color = ALT_ROW if i % 2 == 0 else WHITE

        for col_idx, val in enumerate([i, date_val, time_val, plate_val, status_val], start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.value = val
            cell.font = Font(name="Calibri", size=10)
            cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        # Color-code status cell
        status_cell = ws.cell(row=row_idx, column=5)
        if status_val == "ALLOWED":
            status_cell.font = Font(name="Calibri", size=10, bold=True, color="22543D")
            status_cell.fill = PatternFill(start_color="C6F6D5", end_color="C6F6D5", fill_type="solid")
        else:
            status_cell.font = Font(name="Calibri", size=10, bold=True, color="742A2A")
            status_cell.fill = PatternFill(start_color="FED7D7", end_color="FED7D7", fill_type="solid")

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    filename = f"Gateman_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
