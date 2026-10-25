#!/usr/bin/env python3
"""
AI Gateman — High-Speed Terminal CLI Scanner (30+ FPS Multi-Threaded Engine)
Real-time ANPR directly from your terminal using Apple Silicon GPU!

Usage:
  python cli_scanner.py                    # Live MacBook camera feed (High FPS)
  python cli_scanner.py --image car.jpg    # Scan single image
  python cli_scanner.py --video car.mp4    # Scan video file
  python cli_scanner.py --headless         # Terminal-only mode (no popup window)
"""

import sys
import os
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np

# Ensure backend can be imported
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend.main import (
    car_model, plate_model, reader, DEVICE,
    clean_and_validate_plate, get_whitelist, log_plate_to_db,
    preprocess_plate_crop, cluster_ocr_lines, CAR_MODEL_PATH, PLATE_MODEL_PATH
)

# Terminal ANSI Color Codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Background OCR Thread Pool & Cache
ocr_pool = ThreadPoolExecutor(max_workers=2)
pending_ocr_keys = set()
plate_cache = {} # pos_key -> (recognized_text, status, confidence, timestamp)
last_printed_plates = {} # plate_text -> timestamp

def print_banner():
    print(f"{CYAN}{BOLD}" + "=" * 68 + f"{RESET}")
    print(f"{BOLD} 🚀 AI GATEMAN — HIGH-SPEED ANPR ENGINE (30+ FPS MULTI-THREADED){RESET}")
    print(f" • Device Acceleration: {GREEN}{BOLD}{DEVICE.upper()} (Apple Metal GPU){RESET}")
    print(f" • Vehicle Model:       {YELLOW}{os.path.basename(CAR_MODEL_PATH)}{RESET}")
    print(f" • Plate Model:         {YELLOW}{os.path.basename(PLATE_MODEL_PATH)}{RESET}")
    print(f" • Controls:            Press {BOLD}'q'{RESET} in camera window or {BOLD}Ctrl+C{RESET} to exit")
    print(f"{CYAN}{BOLD}" + "=" * 68 + f"{RESET}\n")

class ThreadedCamera:
    """Non-blocking camera capture thread. Eliminates OpenCV buffer lag."""
    def __init__(self, src=0, width=1280, height=720):
        self.cap = cv2.VideoCapture(src)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.grabbed = False
        self.frame = None
        self.stopped = False

        # Allow slow webcams / macOS privacy prompts up to 1.5s to deliver initial frame
        for _ in range(8):
            self.grabbed, self.frame = self.cap.read()
            if self.grabbed and self.frame is not None:
                break
            time.sleep(0.2)

        if self.grabbed:
            self.thread = threading.Thread(target=self.update, daemon=True)
            self.thread.start()

    def update(self):
        while not self.stopped:
            grabbed, frame = self.cap.read()
            if not grabbed:
                self.stopped = True
            else:
                self.frame = frame

    def read(self):
        return self.grabbed and not self.stopped, self.frame

    def release(self):
        self.stopped = True
        self.cap.release()

def async_ocr_worker(plate_crop, pos_key, whitelist):
    """Background worker: runs EasyOCR without slowing down the video feed."""
    try:
        proc_crop = preprocess_plate_crop(plate_crop)
        ocr_res = reader.readtext(proc_crop, detail=1, paragraph=False, low_text=0.2)

        recognized_plate = None
        ocr_conf = 0.0

        if ocr_res:
            ocr_res = cluster_ocr_lines(ocr_res)
            raw_text = " ".join([r[1] for r in ocr_res])
            conf_avg = sum([r[2] for r in ocr_res]) / len(ocr_res)
            
            validated = clean_and_validate_plate(raw_text, whitelist)
            if validated and conf_avg >= 0.40:
                recognized_plate = validated
                ocr_conf = conf_avg

        if not recognized_plate:
            raw_ocr = reader.readtext(plate_crop, detail=0)
            if raw_ocr:
                val = clean_and_validate_plate("".join(raw_ocr), whitelist)
                if val:
                    recognized_plate = val
                    ocr_conf = 0.70

        if recognized_plate:
            status = "ALLOWED" if recognized_plate in whitelist else "UNKNOWN"
            plate_cache[pos_key] = (recognized_plate, status, ocr_conf, time.time())
            log_plate_to_db(recognized_plate, status)
        else:
            # Mark inconclusive so we don't hammer OCR repeatedly on false boxes
            plate_cache[pos_key] = ("...", "UNKNOWN", 0.0, time.time())

    except Exception as e:
        plate_cache[pos_key] = ("...", "UNKNOWN", 0.0, time.time())
    finally:
        pending_ocr_keys.discard(pos_key)

def match_cached_plate(cx, cy):
    """Matches a bounding box center against recently recognized plates."""
    now = time.time()
    for (kx, ky), (text, status, conf, t_stamp) in list(plate_cache.items()):
        if now - t_stamp > 3.0: # Expire after 3 seconds
            plate_cache.pop((kx, ky), None)
            continue
        # Within 70 pixels = same license plate
        if (cx - kx) ** 2 + (cy - ky) ** 2 < 4900:
            return (text, status, conf)
    return None

def process_frame_fast(frame, whitelist, cached_cars, show_gui=True):
    """Ultra-fast frame processing: GPU plate detection + async OCR."""
    H, W = frame.shape[:2]
    detected_plates_out = []

    # 1. License Plate Detection directly on Apple Silicon GPU
    plate_results = plate_model.predict(frame, verbose=False, imgsz=480, conf=0.35, device=DEVICE)

    for pbox, pconf in zip(plate_results[0].boxes.xyxy, plate_results[0].boxes.conf):
        px1, py1, px2, py2 = map(int, pbox)
        bw = px2 - px1
        bh = py2 - py1
        if bw < 45 or bh < 14:
            continue
        aspect = bw / float(bh)
        if aspect < 1.3 or aspect > 6.0:
            continue

        cx = (px1 + px2) // 2
        cy = (py1 + py2) // 2
        pos_key = (cx // 30 * 30, cy // 30 * 30)

        # Check vehicle classification
        vtype = "Vehicle"
        for cx1, cy1, cx2, cy2, ctype in cached_cars:
            if cx1 <= px1 and cy1 <= py1 and cx2 >= px2 and cy2 >= py2:
                vtype = ctype
                break

        # Check if already recognized in cache
        cached = match_cached_plate(cx, cy)
        if cached:
            text, status, conf = cached
            if text != "...":
                detected_plates_out.append((text, status, conf, vtype))
        else:
            # Dispatch async OCR in background thread if not already running
            text = "SCANNING..."
            status = "PENDING"
            conf = float(pconf)

            if pos_key not in pending_ocr_keys:
                pending_ocr_keys.add(pos_key)
                pad_x = int(bw * 0.06)
                pad_y = int(bh * 0.08)
                qx1 = max(0, px1 - pad_x)
                qy1 = max(0, py1 - pad_y)
                qx2 = min(W, px2 + pad_x)
                qy2 = min(H, py2 + pad_y)
                plate_crop = frame[qy1:qy2, qx1:qx2].copy()
                if plate_crop.size > 0:
                    ocr_pool.submit(async_ocr_worker, plate_crop, pos_key, whitelist)

        # Draw overlays on GUI
        if show_gui:
            if status == "ALLOWED":
                color = (0, 255, 0)
                label = f"{text} [ALLOWED]"
            elif status == "UNKNOWN":
                color = (0, 165, 255)
                label = f"{text} [RESTRICTED]"
            else:
                color = (255, 200, 0)
                label = "ANALYZING PLATE..."

            cv2.rectangle(frame, (px1, py1), (px2, py2), color, 2)
            cv2.rectangle(frame, (px1, max(0, py1 - 24)), (px1 + len(label) * 10 + 10, py1), (15, 23, 42), -1)
            cv2.putText(frame, label, (px1 + 5, max(16, py1 - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, color, 2)

    return frame, detected_plates_out

def main():
    parser = argparse.ArgumentParser(description="AI Gateman - High-Speed Terminal ANPR")
    parser.add_argument("--image", type=str, default=None, help="Path to image file to scan")
    parser.add_argument("--video", type=str, default=None, help="Path to video file to scan")
    parser.add_argument("--camera", type=int, default=0, help="Camera index (default: 0 for built-in webcam)")
    parser.add_argument("--headless", action="store_true", help="Run without opening GUI window")
    args = parser.parse_args()

    print_banner()
    whitelist = get_whitelist()
    print(f"📋 Loaded {len(whitelist)} whitelisted plates: {', '.join(sorted(list(whitelist)))}\n")

    # Mode 1: Single Image Scan
    if args.image:
        if not os.path.exists(args.image):
            print(f"{RED}Error: Image file '{args.image}' not found!{RESET}")
            return
        frame = cv2.imread(args.image)
        t0 = time.time()
        # Synchronous for single image
        from backend.main import detect_objects
        out_frame, plates = process_frame_fast(frame, whitelist, [], show_gui=not args.headless)
        # Wait a moment for background OCR
        time.sleep(0.3)
        out_frame, plates = process_frame_fast(frame, whitelist, [], show_gui=not args.headless)
        latency = (time.time() - t0) * 1000

        print(f"⏱️  Scan completed in {latency:.1f}ms on {DEVICE.upper()} GPU:")
        if plates:
            for p, st, conf, vt in plates:
                color = GREEN if st == "ALLOWED" else RED
                badge = f"{color}{BOLD}[{st}]{RESET}"
                print(f"  • {BOLD}Plate:{RESET} {CYAN}{p}{RESET} | {badge} | {BOLD}Type:{RESET} {vt} | {BOLD}Conf:{RESET} {conf*100:.1f}%")
        else:
            print(f"  {YELLOW}No authorized license plate detected.{RESET}")

        if not args.headless:
            cv2.imshow("Gateman - Image Scan (Press any key to exit)", out_frame)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        return

    # Mode 2: High-Speed Live Camera or Video Feed
    source = args.video if args.video else args.camera
    cam = ThreadedCamera(source, width=1280, height=720)

    grabbed, test_frame = cam.read()
    if not grabbed:
        print(f"{RED}Error: Could not open camera / video source '{source}'{RESET}")
        if not args.video:
            print(f"{YELLOW}💡 Tip for macOS:{RESET} Ensure Terminal / Python has Camera permission in:")
            print(f"   System Settings → Privacy & Security → Camera")
            print(f"   Or run against a video/image file:")
            print(f"   python cli_scanner.py --image your_car.jpg")
        return

    print(f"{GREEN}● High-Speed Live Scanner Active!{RESET} Multi-threaded async engine running.")
    if not args.headless:
        print(f"  (Focus camera window and press {BOLD}'q'{RESET} to stop)\n")

    fps_history = []
    prev_time = time.time()
    frame_idx = 0
    cached_cars = []

    try:
        while True:
            ret, frame = cam.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            frame_idx += 1

            # Run Vehicle detection every 4 frames to keep FPS high (30+ FPS)
            if frame_idx % 4 == 0:
                car_res = car_model.predict(frame, verbose=False, imgsz=480, conf=0.30, device=DEVICE)
                cached_cars = []
                CLASS_NAMES = {2: 'Sedan', 3: 'Motorcycle', 5: 'Bus', 7: 'Truck'}
                for box, cls in zip(car_res[0].boxes.xyxy, car_res[0].boxes.cls):
                    cid = int(cls)
                    if cid in [2, 3, 5, 7]:
                        x1, y1, x2, y2 = map(int, box)
                        cached_cars.append((x1, y1, x2, y2, CLASS_NAMES.get(cid, "Vehicle")))

            # Draw cached vehicles
            if not args.headless:
                for cx1, cy1, cx2, cy2, ctype in cached_cars:
                    cv2.rectangle(frame, (cx1, cy1), (cx2, cy2), (255, 255, 255), 1)
                    cv2.putText(frame, ctype, (cx1, max(15, cy1 - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

            # Ultra-fast plate detection + async OCR
            out_frame, plates = process_frame_fast(frame, whitelist, cached_cars, show_gui=not args.headless)

            now = time.time()
            instant_fps = 1.0 / (now - prev_time) if (now - prev_time) > 0 else 30
            prev_time = now
            fps_history.append(instant_fps)
            if len(fps_history) > 15:
                fps_history.pop(0)
            avg_fps = sum(fps_history) / len(fps_history)

            # Print recognized plates to terminal (deduplicated by 3.5s)
            for p, st, conf, vt in plates:
                if p not in last_printed_plates or (now - last_printed_plates[p]) > 3.5:
                    last_printed_plates[p] = now
                    ts = time.strftime("%H:%M:%S")
                    status_badge = f"{GREEN}{BOLD}✔ ALLOWED (ACCESS GRANTED){RESET}" if st == "ALLOWED" else f"{RED}{BOLD}✘ UNKNOWN (ACCESS RESTRICTED){RESET}"
                    print(f"[{ts}] 🚗 Plate: {CYAN}{BOLD}{p:11}{RESET} | {status_badge} | Vehicle: {vt} | Conf: {conf*100:.0f}% | {avg_fps:.0f} FPS")

            # Render HUD
            if not args.headless:
                cv2.putText(out_frame, f"Gateman GPU Engine | {avg_fps:.0f} FPS | Press 'q' to exit",
                            (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow("AI Gateman - Live Terminal ANPR", out_frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopping scanner...{RESET}")
    finally:
        cam.release()
        ocr_pool.shutdown(wait=False)
        if not args.headless:
            cv2.destroyAllWindows()
        print(f"{GREEN}Scanner stopped cleanly.{RESET}")

if __name__ == "__main__":
    main()
