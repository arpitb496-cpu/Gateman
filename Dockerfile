# Use a lightweight python image
FROM python:3.11-slim

# Install system dependencies required for OpenCV (libgl1) and AI tools
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Configure persistent data directory and volume
ENV GATEMAN_DATA_DIR=/data
VOLUME /data

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files
COPY . .

# Expose port 8000 for FastAPI
EXPOSE 8000

# Start the server
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
