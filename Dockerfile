# ---- Base image ----
FROM python:3.10-slim AS base

# System deps for dlib/face_recognition + Pillow
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential cmake \
    libopenblas-dev liblapack-dev \
    libjpeg-dev zlib1g-dev \
    libboost-all-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Safer, smaller images: only copy what we need first for dependency layer
COPY requirements.txt .

# Upgrade pip and install wheels where possible
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Use a non-root user for safety
RUN useradd -m appuser
USER appuser

# Expose the port Gunicorn will bind to
EXPOSE 5000

# Healthcheck (optional but nice in prod)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -f http://127.0.0.1:5000/ || exit 1

# Start the app with Gunicorn (4 workers, tweak as needed)
CMD ["gunicorn", "--workers=4", "--bind=0.0.0.0:5000", "app:app"]
