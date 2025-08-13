# Use Python 3.9 (face-recognition works best here)
FROM python:3.9-slim

# Install system dependencies for dlib & face-recognition
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libgtk-3-dev \
    libboost-all-dev \
    libatlas-base-dev \
    liblapack-dev \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip

# Install dlib from prebuilt wheel (no compilation)
RUN pip install --no-cache-dir dlib==19.24.2 --only-binary :all:

# Install face-recognition and other dependencies
RUN pip install --no-cache-dir face-recognition face-recognition-models

# Copy your app files
WORKDIR /app
COPY . /app

# Install other requirements if you have a requirements.txt
# RUN pip install --no-cache-dir -r requirements.txt

# Expose port if running a web app
EXPOSE 8000

# Start your app (replace with your actual start command)
CMD ["python", "app.py"]
