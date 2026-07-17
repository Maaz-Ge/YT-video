# Tatterveil Scene Studio — production image (Flask + ffmpeg)
FROM python:3.12-slim-bookworm

# ffmpeg: required for ZIP export (PNG → MP4 chunks)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app.py config.py ./
COPY engine/ ./engine/
COPY templates/ ./templates/
COPY static/ ./static/

# These are created at runtime too; mount host volumes here in production so
# generated data (projects, standalone single images, standalone voices) survives
# image rebuilds and container recreation.
RUN mkdir -p /app/projects /app/singles /app/voices

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5001

# IMPORTANT: use 1 worker only — the app keeps in-memory state (generation status,
# export jobs, regeneration queue). Multiple gunicorn workers would split that state.
# Threads handle concurrent HTTP + background ThreadPoolExecutor work.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "12", \
     "--timeout", "600", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
