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

# SQLite lives under data/ — mount a persistent volume here in production.
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["python", "main.py"]
