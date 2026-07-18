FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (layer cache).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy the application source.
COPY . .

# SQLite lives under data/ — the directory must exist in the image so
# Render's persistent-disk mount at runtime can bind over it. We drop
# `chmod 777` because the volume mount overlays the directory at runtime,
# making the in-image chmod irrelevant.
RUN mkdir -p /app/data

CMD ["python", "main.py"]
