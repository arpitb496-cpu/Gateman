import argparse
import threading
from ultralytics import YOLO
import cv2
import easyocr

# Argument parser to take image path from terminal
parser = argparse.ArgumentParser(description="YOLO Object and License Plate Detection")
parser.add_argument('image_path', type=str, help="Path to the image file")
args = parser.parse_args()

# Initialize EasyOCR reader
reader = easyocr.Reader(['en'], gpu=False)  # Disable GPU on Pi

# Load the YOLO models once
model1 = YOLO('../resources/yolov8n.pt')  # General object detection
model2 = YOLO('../resources/kbest.pt')    # License plate detection

def ocr_plate(cropped_plate_gray):
    """Run OCR and print result"""
    ocr_result = reader.readtext(cropped_plate_gray)
    if ocr_result:
        for (_, text, confidence) in ocr_result:
            text_no_spaces = text.replace(" ", "")
            print(f"Detected Plate: {text_no_spaces} ({confidence*100:.1f}%)")
    else:
        print("No text detected.")

def detect_license_plates(img, image_path=None):
    """Detect license plates in image and run OCR"""
    # Use numpy image directly if available
    results = model2(img if img is not None else image_path)
    boxes = results[0].boxes

    if boxes is None or not boxes.xyxy.size(0):
        print("No license plate found.")
        return

    print(f"{len(boxes.xyxy)} License Plate(s) Found:")
    for i, box in enumerate(boxes.xyxy):
        x1, y1, x2, y2 = map(int, box)
        cropped = img[y1:y2, x1:x2]

        try:
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (160, 50))
            threading.Thread(target=ocr_plate, args=(gray,)).start()
        except Exception as e:
            print(f"Plate {i+1}: Error in OCR preprocessing: {e}")

def main():
    image_path = args.image_path
    img = cv2.imread(image_path)

    if img is None:
        print("Error: Image not found.")
        return

    # Run general detection and plate detection in parallel
    thread1 = threading.Thread(target=lambda: model1(img))
    thread2 = threading.Thread(target=detect_license_plates, args=(img,))

    thread1.start()
    thread2.start()

    thread1.join()
    thread2.join()

if __name__ == "__main__":
    main()
