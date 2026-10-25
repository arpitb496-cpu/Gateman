import os
import sys
import io
import csv
import re
import time
import shutil
import tempfile
import hmac
import threading
from datetime import datetime
from typing import Optional, Iterable
from contextlib import asynccontextmanager
from PIL import Image

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
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

CAR_MODEL_PATH = find_file("yolov8s.pt") if os.path.exists(find_file("yolov8s.pt")) else find_file("yolov8n.pt")
PLATE_MODEL_PATH = find_file("kbest.pt")

def get_data_dir() -> str:
    """Returns directory configured by GATEMAN_DATA_DIR env var, defaulting to PROJECT_ROOT."""
    data_dir = os.environ.get("GATEMAN_DATA_DIR")
    if data_dir and data_dir.strip():
        path = os.path.abspath(data_dir.strip())
        os.makedirs(path, exist_ok=True)
        return path
    return PROJECT_ROOT

def get_whitelist_file() -> str:
    return os.path.join(get_data_dir(), "whitelist.txt")

def get_log_file() -> str:
    return os.path.join(get_data_dir(), "detection_log.csv")

def get_audit_log_file() -> str:
    return os.path.join(get_data_dir(), "audit_log.csv")

def __getattr__(name: str):
    if name == "WHITELIST_FILE":
        return get_whitelist_file()
    if name == "LOG_FILE":
        return get_log_file()
    if name == "AUDIT_LOG_FILE":
        return get_audit_log_file()
    if name == "DATA_DIR":
        return get_data_dir()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

def get_admin_token() -> str:
    token = os.environ.get("GATEMAN_ADMIN_TOKEN", "")
    if not token or len(token) < 16:
        raise RuntimeError("GATEMAN_ADMIN_TOKEN environment variable must be set and at least 16 characters long.")
    return token

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_admin_token()
    yield

app = FastAPI(title="Automatic Gateman API - Apple Silicon GPU Accelerated", lifespan=lifespan)

# CORS setup (W3C compliant)
# The dashboard is served from port 3000 and calls the API on port 8000, so the default CORS origin list must include http://localhost:3000
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
DEFAULT_ORIGINS = DEFAULT_CORS_ORIGINS

cors_origins_env = os.environ.get("CORS_ORIGINS", "")
if cors_origins_env:
    CORS_ORIGINS = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    if "http://localhost:3000" not in CORS_ORIGINS:
        CORS_ORIGINS.append("http://localhost:3000")
    if "http://127.0.0.1:3000" not in CORS_ORIGINS:
        CORS_ORIGINS.append("http://127.0.0.1:3000")
else:
    CORS_ORIGINS = list(DEFAULT_CORS_ORIGINS)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Allow preflight CORS OPTIONS requests through
        if request.method == "OPTIONS":
            return await call_next(request)

        # GET /health is explicitly exempt from authentication
        if request.url.path.rstrip("/") == "/health" and request.method == "GET":
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing authentication token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:].strip()
        try:
            admin_token = get_admin_token()
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": str(e)},
            )

        if not hmac.compare_digest(token, admin_token):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing authentication token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)

# Register AuthMiddleware first (innermost layer)
app.add_middleware(AuthMiddleware)

# Register CORSMiddleware last (outermost layer so all responses, including 401/500, get CORS headers)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "*"],
)

_whitelist_lock = threading.RLock()
_audit_lock = threading.Lock()
_csv_lock = threading.Lock()

class WhitelistPlateRequest(BaseModel):
    plate: str

def get_trusted_proxy_ips() -> set:
    raw = os.environ.get("TRUSTED_PROXY_IPS", "")
    if not raw:
        return set()
    return {ip.strip() for ip in raw.split(",") if ip.strip()}

def get_client_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client and request.client.host else "127.0.0.1"
    trusted_proxies = get_trusted_proxy_ips()
    if peer_ip in trusted_proxies:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            if client_ip:
                return client_ip
    return peer_ip

def log_audit(action: str, detail: str, client_ip: str = "127.0.0.1") -> None:
    timestamp = datetime.now().isoformat()
    audit_file = get_audit_log_file()
    with _audit_lock:
        needs_header = not os.path.exists(audit_file) or os.path.getsize(audit_file) == 0
        with open(audit_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if needs_header:
                writer.writerow(["timestamp", "action", "detail", "client_ip"])
            writer.writerow([timestamp, action, detail, client_ip])

def save_whitelist_atomic(plates: Iterable[str]) -> None:
    """Atomically writes sorted unique whitelist plates using temp file + os.replace under lock."""
    with _whitelist_lock:
        whitelist_file = get_whitelist_file()
        dir_name = os.path.dirname(os.path.abspath(whitelist_file))
        fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="whitelist_", suffix=".tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for p in sorted(list(set(plates))):
                    f.write(f"{p}\n")
            os.replace(temp_path, whitelist_file)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

def init_whitelist():
    """Initializes the whitelist file with an empty list if not present, logging a warning."""
    with _whitelist_lock:
        whitelist_file = get_whitelist_file()
        if not os.path.exists(whitelist_file):
            print(f"⚠️ Warning: Whitelist file not found at {whitelist_file}; initializing empty whitelist.")
            save_whitelist_atomic([])

def get_whitelist():
    """Reads whitelisted plate numbers."""
    init_whitelist()
    whitelist_file = get_whitelist_file()
    plates = set()
    with _whitelist_lock:
        try:
            with open(whitelist_file, "r", encoding="utf-8") as f:
                for line in f:
                    p = re.sub(r'[^A-Z0-9]', '', line.strip().upper())
                    if p:
                        plates.add(p)
        except Exception as e:
            print(f"Error reading whitelist: {e}")
    return plates

_last_logged_plates = {}

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

    log_file = get_log_file()
    with _csv_lock:
        file_exists = os.path.exists(log_file)
        try:
            with open(log_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists or os.path.getsize(log_file) == 0:
                    writer.writerow(["Date", "Time", "Plate Number", "Status"])
                writer.writerow([date_str, time_str, plate_text, status])
        except Exception as e:
            print(f"CSV log error: {e}")

# Hardware Acceleration Setup: cuda -> mps -> cpu
import torch

def select_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"

DEVICE = select_device()
USE_GPU = DEVICE in ("cuda", "mps")

# Load AI Models (Fail Closed)
ALLOW_NO_MODELS = os.environ.get("GATEMAN_ALLOW_NO_MODELS") == "1"
MODELS_LOADED = False
reader = None
car_model = None
plate_model = None

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

if ALLOW_NO_MODELS:
    print("⚠️ GATEMAN_ALLOW_NO_MODELS=1: Starting without AI models (degraded mode for tests).")
else:
    try:
        import easyocr
        from ultralytics import YOLO

        device_name = "NVIDIA CUDA GPU" if DEVICE == "cuda" else ("Apple Silicon Metal (MPS) GPU" if DEVICE == "mps" else "Multi-core CPU")
        print(f"🚀 Initializing AI Models on {device_name} (device={DEVICE})...")
        reader = easyocr.Reader(['en'], gpu=USE_GPU, verbose=False)
        car_model = YOLO(CAR_MODEL_PATH)
        plate_model = YOLO(PLATE_MODEL_PATH)

        # Warm-up models on GPU for zero-latency live inference
        if np is not None:
            warmup_img = np.zeros((320, 320, 3), dtype=np.uint8)
            car_model.predict(warmup_img, device=DEVICE, verbose=False)
            plate_model.predict(warmup_img, device=DEVICE, verbose=False)
            reader.readtext(warmup_img[100:200, 100:200])

        print(f"✅ Active: Vehicle Model [{os.path.basename(CAR_MODEL_PATH)}] & Plate Model [{os.path.basename(PLATE_MODEL_PATH)}] on {DEVICE.upper()} GPU!")
        MODELS_LOADED = True
    except Exception as e:
        print(f"❌ Fatal: Failed to initialize AI models: {e}", file=sys.stderr)
        sys.exit(1)

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

from typing import NamedTuple, Optional, Iterable

class ValidationResult(NamedTuple):
    plate: Optional[str] = None
    status: Optional[str] = None

    def __bool__(self):
        return self.plate is not None

    def __eq__(self, other):
        if isinstance(other, str):
            return self.plate == other
        return tuple.__eq__(self, other)

OCR_CONFUSIONS = {
    ('0', 'O'), ('O', '0'),
    ('0', 'Q'), ('Q', '0'),
    ('1', 'I'), ('I', '1'),
    ('1', 'L'), ('L', '1'),
    ('2', 'Z'), ('Z', '2'),
    ('5', 'S'), ('S', '5'),
    ('6', 'G'), ('G', '6'),
    ('8', 'B'), ('B', '8'),
}

def is_ocr_fuzzy_match(candidate: str, target: str) -> bool:
    """
    Returns True iff candidate and target are of the exact same length,
    differ by at least 1 character, and EVERY differing character pair
    (candidate[i], target[i]) is a known OCR confusion pair.
    No insertions or deletions are allowed.
    """
    if len(candidate) != len(target):
        return False
    if candidate == target:
        return False
    diff_count = 0
    for c1, c2 in zip(candidate, target):
        if c1 != c2:
            if (c1, c2) not in OCR_CONFUSIONS:
                return False
            diff_count += 1
    return diff_count > 0

def clean_and_validate_plate(text, whitelist=None) -> ValidationResult:
    """
    High-Precision Indian License Plate Validator & OCR Correction:
    - Exact match against whitelist -> status: ALLOWED
    - Fuzzy matching applies ONLY to strings of the exact same length where
      EVERY differing character pair is a known OCR confusion pair (0/O, 0/Q, 1/I, 1/L, 2/Z, 5/S, 6/G, 8/B).
      No insertions or deletions allowed.
    - If exactly one whitelist plate matches fuzzy criteria -> status: NEEDS_CONFIRM (corrected plate returned)
    - If more than one whitelist plate matches -> return no match (ambiguous).
    - If valid Indian plate format (RTO or BH series) not in whitelist -> status: UNKNOWN
    - If not a valid plate -> ValidationResult(None, None)
    """
    if not text:
        return ValidationResult(None, None)

    raw = re.sub(r'[^A-Z0-9]', '', str(text).upper())
    if not raw:
        return ValidationResult(None, None)

    # Clean whitelist plates
    clean_wl = set()
    if whitelist:
        for p in whitelist:
            wp = re.sub(r'[^A-Z0-9]', '', str(p).upper())
            if wp:
                clean_wl.add(wp)

    # Candidate strings to evaluate (raw, and stripped of state headers or 'IND' stamps)
    candidates = [raw]
    for word in COMMON_PREFIX_WORDS:
        if raw.startswith(word) and len(raw) >= len(word) + 7:
            candidates.append(raw[len(word):])
            break
    if raw.startswith("IND") and len(raw) >= 9:
        candidates.append(raw[3:])

    # 1. Exact match against whitelist = ALLOWED
    if clean_wl:
        for cand in candidates:
            if cand in clean_wl:
                return ValidationResult(cand, "ALLOWED")

    # 2. Fuzzy matching against whitelist:
    # - Same length only (no insertions or deletions)
    # - Every differing character pair must be a known OCR confusion
    # - If more than one whitelist plate matches, return no match
    # - A fuzzy-corrected read returns status NEEDS_CONFIRM, not ALLOWED
    if clean_wl:
        for cand in candidates:
            fuzzy_matches = [wp for wp in clean_wl if is_ocr_fuzzy_match(cand, wp)]
            if len(fuzzy_matches) == 1:
                return ValidationResult(fuzzy_matches[0], "NEEDS_CONFIRM")
            elif len(fuzzy_matches) > 1:
                # Ambiguous: multiple whitelist plates match -> return no match
                break

    # 3. Generic Plate Validation (Format check for unregistered/unknown vehicles)
    for cand in candidates:
        if len(cand) < 7 or len(cand) > 11:
            continue

        # (a) Check BH-Series first:
        # Format: [2 digits Year] BH [4 digits Number] [1-2 letters Series]
        # Detect BH format first and skip state-letter normalization so digits in year (e.g. 22) don't map to letters.
        if len(cand) in (9, 10) and (cand[2:4] == "BH" or cand[2:4] == "8H"):
            y0 = CHAR_TO_NUM.get(cand[0], cand[0])
            y1 = CHAR_TO_NUM.get(cand[1], cand[1])
            num_part = "".join(CHAR_TO_NUM.get(c, c) for c in cand[4:8])
            ser_part = "".join(NUM_TO_CHAR.get(c, c) for c in cand[8:])
            bh_plate = f"{y0}{y1}BH{num_part}{ser_part}"
            if re.match(r'^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$', bh_plate):
                return ValidationResult(bh_plate, "UNKNOWN")

        # (b) Standard Indian Plate Structural Parser:
        # [State letters: 2] [RTO: 1 or 2 digits] [Series: 1-3 letters] [Number: 3-4 digits]
        # Try both 1-digit and 2-digit RTO interpretations and keep the one that fits.
        # If both fit, prefer the one with no (fewest) character changes.
        state_raw = cand[0:2]
        s0 = NUM_TO_CHAR.get(state_raw[0], state_raw[0])
        s1 = NUM_TO_CHAR.get(state_raw[1], state_raw[1])
        if state_raw in INDIAN_STATES:
            state = state_raw
            state_changes = 0
        elif (s0 + s1) in INDIAN_STATES:
            state = s0 + s1
            state_changes = (s0 != state_raw[0]) + (s1 != state_raw[1])
        else:
            continue

        rest = cand[2:]
        L = len(rest)
        valid_interpretations = []

        for rto_len in (1, 2):
            if L <= rto_len + 3:
                continue

            raw_rto = rest[:rto_len]
            after_rto = rest[rto_len:]
            M = len(after_rto)

            # Series length can be 1, 2, or 3
            for ser_len in (1, 2, 3):
                num_len = M - ser_len
                if num_len not in (3, 4):
                    continue

                raw_ser = after_rto[:ser_len]
                raw_num = after_rto[ser_len:]

                # 1. RTO digits validation & normalization
                rto_chars = []
                rto_changes = 0
                rto_valid = True
                for c in raw_rto:
                    if c.isdigit():
                        rto_chars.append(c)
                    elif c in CHAR_TO_NUM and CHAR_TO_NUM[c].isdigit():
                        rto_chars.append(CHAR_TO_NUM[c])
                        rto_changes += 1
                    else:
                        rto_valid = False
                        break
                if not rto_valid:
                    continue

                # 2. Series letters validation & normalization
                # Indian registration marks strictly omit 'I' and 'O' to avoid confusion with 1 and 0.
                ser_chars = []
                ser_changes = 0
                ser_valid = True
                for c in raw_ser:
                    if c.isalpha() and c not in ('O', 'I'):
                        ser_chars.append(c)
                    elif c in NUM_TO_CHAR and NUM_TO_CHAR[c] not in ('O', 'I'):
                        ser_chars.append(NUM_TO_CHAR[c])
                        ser_changes += 1
                    else:
                        ser_valid = False
                        break
                if not ser_valid:
                    continue

                # 3. Number digits validation & normalization
                num_chars = []
                num_changes = 0
                num_valid = True
                for c in raw_num:
                    if c.isdigit():
                        num_chars.append(c)
                    elif c in CHAR_TO_NUM and CHAR_TO_NUM[c].isdigit():
                        num_chars.append(CHAR_TO_NUM[c])
                        num_changes += 1
                    else:
                        num_valid = False
                        break
                if not num_valid:
                    continue

                total_changes = state_changes + rto_changes + ser_changes + num_changes
                rto_str = "".join(rto_chars)
                ser_str = "".join(ser_chars)
                num_str = "".join(num_chars)
                plate_candidate = f"{state}{rto_str}{ser_str}{num_str}"

                if re.match(r'^([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{3,4})$', plate_candidate):
                    valid_interpretations.append({
                        "changes": total_changes,
                        "rto_len": rto_len,
                        "plate": plate_candidate
                    })

        if valid_interpretations:
            # Sort by fewest character changes (prefer 0 changes if both fit)
            valid_interpretations.sort(key=lambda x: x["changes"])
            return ValidationResult(valid_interpretations[0]["plate"], "UNKNOWN")

    return ValidationResult(None, None)

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
    if not MODELS_LOADED or car_model is None or plate_model is None or reader is None:
        raise HTTPException(
            status_code=503,
            detail="AI detection models not loaded (system offline)"
        )

    try:
        contents = file.file.read()
        image = Image.open(io.BytesIO(contents)).convert('RGB')
        width, height = image.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {str(e)}")

    whitelist = get_whitelist()

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
        plate_status = "UNKNOWN"
        ocr_conf = 0.0

        if ocr_res:
            ocr_res = cluster_ocr_lines(ocr_res)
            raw_text = " ".join([r[1] for r in ocr_res])
            conf_sum = sum([r[2] for r in ocr_res]) / len(ocr_res)
            
            val_res = clean_and_validate_plate(raw_text, whitelist)
            if val_res and val_res.plate and conf_sum >= 0.35:
                recognized_text = val_res.plate
                plate_status = val_res.status
                ocr_conf = conf_sum

        # Robust Fallback: If enhanced grayscale didn't produce a valid plate, try raw crop
        if recognized_text == "...":
            raw_ocr = reader.readtext(plate_crop, detail=0)
            if raw_ocr:
                val_res = clean_and_validate_plate("".join(raw_ocr), whitelist)
                if val_res and val_res.plate:
                    recognized_text = val_res.plate
                    plate_status = val_res.status
                    ocr_conf = 0.70

        # Only register plate if recognized or if YOLO plate detector is confident (>0.40)
        if recognized_text != "..." or pconf >= 0.40:
            plates_detected.append({
                "box": [px1, py1, px2, py2],
                "text": recognized_text,
                "confidence": round(float(ocr_conf if recognized_text != "..." else pconf), 2),
                "vehicle_type": vtype,
                "status": plate_status
            })

    # 4. Check whitelist for status and log valid reads
    for plate in plates_detected:
        plate_str = plate["text"]
        if plate_str != "...":
            clean_str = re.sub(r'[^A-Z0-9]', '', plate_str.upper())
            status = plate.get("status") or ("ALLOWED" if clean_str in whitelist else "UNKNOWN")
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
        "status": "ok" if MODELS_LOADED else "degraded",
        "device": DEVICE,
        "gpu": USE_GPU,
        "platform": "Apple Silicon (macOS)" if DEVICE == "mps" else ("NVIDIA CUDA" if DEVICE == "cuda" else "CPU"),
        "models": {
            "vehicle": os.path.basename(CAR_MODEL_PATH) if MODELS_LOADED else None,
            "plate": os.path.basename(PLATE_MODEL_PATH) if MODELS_LOADED else None
        }
    }

@app.get("/whitelist")
def get_whitelist_api():
    return {"whitelist": sorted(list(get_whitelist()))}

@app.post("/whitelist/add")
def add_to_whitelist(payload: WhitelistPlateRequest, request: Request):
    clean = re.sub(r'[^A-Z0-9]', '', payload.plate.upper())
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid plate number")
    client_ip = get_client_ip(request)
    with _whitelist_lock:
        wl = get_whitelist()
        if clean not in wl:
            wl.add(clean)
            save_whitelist_atomic(wl)
    log_audit("whitelist_add", clean, client_ip)
    return {"status": "added", "plate": clean}

@app.post("/whitelist/remove")
def remove_from_whitelist(payload: WhitelistPlateRequest, request: Request):
    clean = re.sub(r'[^A-Z0-9]', '', payload.plate.upper())
    if not clean:
        raise HTTPException(status_code=400, detail="Invalid plate number")
    client_ip = get_client_ip(request)
    with _whitelist_lock:
        wl = get_whitelist()
        if clean in wl:
            wl.remove(clean)
            save_whitelist_atomic(wl)
            log_audit("whitelist_remove", clean, client_ip)
            return {"status": "removed", "plate": clean}
    return {"status": "not_found", "plate": clean}

@app.get("/logs")
def get_logs(limit: int = 50):
    """Returns recent detection log entries."""
    rows = []
    log_file = get_log_file()
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
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
def clear_logs(request: Request):
    """Archives detection_log.csv to a timestamped file before resetting it, logging to audit trail."""
    global _last_logged_plates
    client_ip = get_client_ip(request)
    log_file = get_log_file()
    with _csv_lock:
        archive_filename = ""
        try:
            if os.path.exists(log_file):
                dir_name = os.path.dirname(os.path.abspath(log_file))
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                archive_filename = f"detection_log_{timestamp}.csv"
                archive_path = os.path.join(dir_name, archive_filename)
                shutil.copy2(log_file, archive_path)

            with open(log_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Date", "Time", "Plate Number", "Status"])
            _last_logged_plates.clear()

            log_audit("clear_logs", archive_filename or "reset_empty", client_ip)
            return {
                "status": "success",
                "message": "Detection logs cleared",
                "archive": archive_filename
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
def get_stats():
    """Returns aggregate dashboard metrics."""
    total_scans = 0
    unique_plates = set()
    allowed_count = 0
    unknown_count = 0

    log_file = get_log_file()
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
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

    log_file = get_log_file()
    if not rows and os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
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
