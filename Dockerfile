FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
#   ffmpeg          - required by pydub for audio transcription
#   libimage-exiftool-perl - required by markitdown for image/audio metadata
#   libmagic1       - content type sniffing
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    ffmpeg \
    libimage-exiftool-perl \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application files
COPY . .

# Expose the port
EXPOSE 8000

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# One image, two roles. The default is the API (producer); worker pods override
# the command with ["python", "-m", "worker"] to run the consumer instead.
CMD ["uvicorn", "api.index:app", "--host", "0.0.0.0", "--port", "8000"]
