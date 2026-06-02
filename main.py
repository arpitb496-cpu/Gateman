import uvicorn
import os
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import threading
import queue
import time
import io
import csv
import re
from datetime import datetime
from PIL import Image
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from supabase import create_client, Client

app = FastAPI(title="Automatic Gateman API")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Connected to Supabase Cloud Database!")
    except Exception as e:
        print(f"Supabase connection failed: {e}")

def clean_and_validate_plate(text, strict=False):
    """Two-tier plate validator.
    Tier 1: Try to match Indian plate format (most specific).
    Tier 2: Accept any clean alphanumeric string of 4-12 chars
            that has at least 1 letter and 1 digit (universal fallback).
    """
    cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())
    
    if len(cleaned) < 4:
        return None

    # --- Tier 1: Indian plate formats ---
    valid_states = 'AP|AR|AS|BR|CG|GA|GJ|HR|HP|JH|KA|KL|MP|MH|MN|ML|MZ|NL|OD|PB|RJ|SK|TN|TS|TR|UP|UK|WB|AN|CH|DN|DD|DL|JK|LA|LD|PY'
    match = re.search(f'({valid_states})[A-Z0-9]{{4,9}}', cleaned)
    if match and sum(c.isdigit() for c in match.group(0)) >= 2:
        return match.group(0)
    bh_match = re.search(r'\d{2}BH[A-Z0-9]{4,6}', cleaned)
    if bh_match and sum(c.isdigit() for c in bh_match.group(0)) >= 3:
        return bh_match.group(0)
    
    # --- Tier 2: Universal plate fallback (any country) ---
    # Accept any 4-12 char string that has at least 1 letter AND 1 digit
    # and isn't a common OCR false-positive (all same character, etc.)
    if not strict and 4 <= len(cleaned) <= 12:
        has_letter = any(c.isalpha() for c in cleaned)
        has_digit = any(c.isdigit() for c in cleaned)
        # Reject if all characters are the same (e.g. "0000", "AAAA")
        all_same = len(set(cleaned)) <= 1
        if has_letter and has_digit and not all_same:
            return cleaned
    
    return None

# Allow CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Constants & Paths
LOG_FILE = "detection_log.csv"

def log_plate_to_db(plate_text):
    """Appends recognized plate to Supabase (if configured) or local CSV fallback."""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    if supabase:
        try:
            supabase.table("detection_log").insert({
                "date": date_str,
                "time": time_str,
                "plate_number": plate_text
            }).execute()
            return
        except Exception as e:
            print(f"Failed to log to Supabase: {e}")
    
    # Fallback to local CSV if Supabase isn't configured
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Time", "Plate Number"])
        writer.writerow([date_str, time_str, plate_text])

# Attempt to load AI Models, fallback to mock mode if DLLs are missing
USE_MOCK = False
# Global variables for background inference
inference_queue = queue.Queue()
result_dict = {}
req_id_counter = 0

class DetectionResult(BaseModel):
    cars: list[dict]
    plates: list[dict]

def preprocess_for_ocr(plate_crop, strategy='clahe', cv2=None):
    """Multi-strategy OCR preprocessing.
    strategy options: 'clahe', 'thresh', 'sharpen'
    Returns a preprocessed grayscale image optimised for EasyOCR.
    """
    if cv2 is None: import cv2
    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
    
    h, w = gray.shape[:2]
    # Upscale tiny crops so OCR can see details clearly
    target_w = 200
    if w < target_w and w > 0:
        scale = target_w / w
        gray = cv2.resize(gray, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    if strategy == 'clahe':
        # CLAHE: best for uneven lighting / dark plates
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
        gray = clahe.apply(gray)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

    elif strategy == 'thresh':
        # Adaptive threshold: great for high-contrast white plates
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        gray = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 15, 8
        )

    elif strategy == 'sharpen':
        # Unsharp mask: sharpens blurry plates (e.g. from screen glare)
        blur = cv2.GaussianBlur(gray, (0, 0), 3)
        gray = cv2.addWeighted(gray, 1.8, blur, -0.8, 0)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

    return gray


def run_ocr_multipass(plate_crop, reader):
    """Runs OCR with a single fast strategy and universal validation."""
    import cv2
    gray = preprocess_for_ocr(plate_crop, strategy='clahe', cv2=cv2)
    try:
        results = reader.readtext(gray, detail=1, paragraph=False, low_text=0.2)
        if results:
            raw_text = ''.join([r[1] for r in results])
            avg_conf = sum([r[2] for r in results]) / len(results)
            valid = clean_and_validate_plate(raw_text, strict=False)
            if valid:
                return valid, avg_conf
    except Exception:
        pass

    return None, 0.0

def inference_worker():
    import requests
    while True:
        req_id, contents = inference_queue.get()
        try:
            image = Image.open(io.BytesIO(contents)).convert('RGB')
            width, height = image.size
            cx, cy = width // 2, height // 2

            # ULTIMATE FALLBACK: Free Cloud OCR API (Bypasses all local CPU AVX2 crashes)
            payload = {
                'isOverlayRequired': False,
                'apikey': 'helloworld',
                'language': 'eng',
                'OCREngine': 2
            }
            
            print("[DEBUG] Sending image to Cloud OCR...")
            r = requests.post(
                'https://api.ocr.space/parse/image',
                files={'file': ('image.jpg', contents, 'image/jpeg')},
                data=payload,
                timeout=15
            )
            
            res_json = r.json()
            plates_detected = []
            
            if not res_json.get('IsErroredOnProcessing', True):
                raw_text = res_json['ParsedResults'][0]['ParsedText']
                all_text = raw_text.upper()
                cleaned_all = re.sub(r'[^A-Z0-9]', '', all_text)
                
                valid_states = 'AP|AR|AS|BR|CG|GA|GJ|HR|HP|JH|KA|KL|MP|MH|MN|ML|MZ|NL|OD|PB|RJ|SK|TN|TS|TR|UP|UK|WB|AN|CH|DN|DD|DL|JK|LA|LD|PY'
                match = re.search(f'({valid_states})[0-9]{{1,2}}[A-Z]{{1,3}}[0-9]{{4}}', cleaned_all)
                
                if match:
                    found_plate = match.group(0)
                    log_plate_to_db(found_plate)
                    plates_detected.append({
                        "box": [cx - 150, cy - 50, cx + 150, cy + 50],
                        "text": found_plate,
                        "confidence": 0.99,
                        "vehicle_type": "Unknown"
                    })
            
            result_dict[req_id] = DetectionResult(
                cars=[], 
                plates=plates_detected
            )
        except Exception as e:
            result_dict[req_id] = e
        finally:
            inference_queue.task_done()

threading.Thread(target=inference_worker, daemon=True).start()

@app.post("/detect", response_model=DetectionResult)
def detect_objects(file: UploadFile = File(...)):
    global req_id_counter
    req_id = req_id_counter
    req_id_counter += 1
    
    contents = file.file.read()
    inference_queue.put((req_id, contents))
    
    # Wait for worker thread to process image
    while req_id not in result_dict:
        time.sleep(0.05)
        
    res = result_dict.pop(req_id)
    if isinstance(res, Exception):
        raise HTTPException(status_code=500, detail=f"Inference error: {str(res)}")
    return res

@app.get("/health")
def health_check():
    return {"status": "ok", "mode": "mock" if USE_MOCK else "ai"}

@app.get("/export-excel")
def export_excel():
    """Generates a premium styled Excel report from the detection log."""
    from openpyxl.styles import Side, Border
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Detection Log"

    # ── Colour palette ────────────────────────────────────────────────────────
    DARK_BLUE   = "1A365D"
    MID_BLUE    = "2B6CB0"
    LIGHT_BLUE  = "EBF4FF"
    ALT_ROW     = "F7FAFC"
    WHITE       = "FFFFFF"
    ACCENT_GOLD = "D69E2E"
    TEXT_DARK   = "1A202C"
    BORDER_CLR  = "CBD5E0"

    thin_side   = Side(style="thin",   color=BORDER_CLR)
    thick_side  = Side(style="medium", color=MID_BLUE)
    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    thick_border= Border(left=thick_side, right=thick_side, top=thick_side, bottom=thick_side)

    # ── Row 1–2: Big title banner ─────────────────────────────────────────────
    ws.merge_cells("A1:E2")
    title_cell = ws["A1"]
    title_cell.value = "AUTOMATIC GATEMAN - DETECTION REPORT"
    title_cell.font = Font(name="Calibri", size=18, bold=True, color=WHITE)
    title_cell.fill = PatternFill(start_color=DARK_BLUE, end_color=DARK_BLUE, fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 22

    # ── Row 3: Generated timestamp ────────────────────────────────────────────
    ws.merge_cells("A3:E3")
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    sub_cell = ws["A3"]
    sub_cell.value = f"Generated on: {now_str}"
    sub_cell.font = Font(name="Calibri", size=10, italic=True, color=WHITE)
    sub_cell.fill = PatternFill(start_color=MID_BLUE, end_color=MID_BLUE, fill_type="solid")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 16

    # ── Row 4: Spacer ─────────────────────────────────────────────────────────
    ws.row_dimensions[4].height = 8
    ws.append([""])  # empty spacer row

    # ── Row 5: Column Headers ─────────────────────────────────────────────────
    headers = ["#", "Date", "Time", "Number Plate", "Day of Week"]
    ws.append(headers)
    header_row_idx = 5
    col_widths     = [5, 14, 12, 18, 16]

    for col_idx, (hdr, width) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=header_row_idx, column=col_idx)
        cell.value = hdr
        cell.font = Font(name="Calibri", size=11, bold=True, color=WHITE)
        cell.fill = PatternFill(start_color=MID_BLUE, end_color=MID_BLUE, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thick_border
        ws.column_dimensions[chr(64 + col_idx)].width = width
    ws.row_dimensions[header_row_idx].height = 20

    # ── Read data ─────────────────────────────────────────────────────────────
    rows = []
    if supabase:
        try:
            response = supabase.table("detection_log").select("*").execute()
            for r in response.data:
                rows.append((r.get("date", ""), r.get("time", ""), r.get("plate_number", "")))
        except Exception as e:
            print(f"Supabase export error: {e}")

    if not rows and os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            csv_reader = csv.reader(f)
            next(csv_reader, None)
            for row in csv_reader:
                if len(row) >= 3:
                    rows.append((row[0], row[1], row[2]))

    # ── Write data rows ───────────────────────────────────────────────────────
    for i, (date_val, time_val, plate_val) in enumerate(rows, start=1):
        row_idx   = header_row_idx + i
        fill_color = ALT_ROW if i % 2 == 0 else WHITE
        try:
            dow = datetime.strptime(date_val, "%Y-%m-%d").strftime("%A")
        except Exception:
            dow = ""

        for col_idx, val in enumerate([i, date_val, time_val, plate_val, dow], start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.value = val
            cell.font = Font(name="Calibri", size=10, color=TEXT_DARK)
            cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        # Bold the plate number
        ws.cell(row=row_idx, column=4).font = Font(name="Calibri", size=10, bold=True, color=DARK_BLUE)
        ws.row_dimensions[row_idx].height = 16

    # ── Summary section ───────────────────────────────────────────────────────
    total_rows   = len(rows)
    last_data_row = header_row_idx + total_rows + 1
    ws.row_dimensions[last_data_row].height = 10

    summary_start = last_data_row + 1
    ws.merge_cells(f"A{summary_start}:E{summary_start}")
    sum_title = ws[f"A{summary_start}"]
    sum_title.value = "SUMMARY"
    sum_title.font = Font(name="Calibri", size=11, bold=True, color=WHITE)
    sum_title.fill = PatternFill(start_color=ACCENT_GOLD, end_color=ACCENT_GOLD, fill_type="solid")
    sum_title.alignment = Alignment(horizontal="center")
    ws.row_dimensions[summary_start].height = 18

    stats = [
        ("Total Scans",    total_rows),
        ("Unique Plates",  len(set(r[2] for r in rows))),
        ("Report Date",    datetime.now().strftime("%d %b %Y")),
    ]
    for j, (label, val) in enumerate(stats):
        r_idx = summary_start + 1 + j
        ws.merge_cells(f"A{r_idx}:C{r_idx}")
        lc = ws[f"A{r_idx}"]
        lc.value = label
        lc.font = Font(name="Calibri", size=10, bold=True, color=TEXT_DARK)
        lc.fill = PatternFill(start_color=LIGHT_BLUE, end_color=LIGHT_BLUE, fill_type="solid")
        lc.alignment = Alignment(horizontal="left")
        lc.border = thin_border

        ws.merge_cells(f"D{r_idx}:E{r_idx}")
        vc = ws[f"D{r_idx}"]
        vc.value = val
        vc.font = Font(name="Calibri", size=10, bold=True, color=MID_BLUE)
        vc.fill = PatternFill(start_color=LIGHT_BLUE, end_color=LIGHT_BLUE, fill_type="solid")
        vc.alignment = Alignment(horizontal="center")
        vc.border = thin_border
        ws.row_dimensions[r_idx].height = 16

    # ── Auto Filter + Freeze Panes ────────────────────────────────────────────
    if total_rows > 0:
        ws.auto_filter.ref = f"A{header_row_idx}:E{header_row_idx + total_rows}"
    ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1)

    # ── Stream response ───────────────────────────────────────────────────────
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    filename = f"Gateman_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
