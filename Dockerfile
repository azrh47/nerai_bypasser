FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (layer cache).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Sanity-check the install: fail the Docker build loudly with a clear
# log line if discord.py didn't actually land in site-packages. This
# catches the "Build logs say Successfully installed discord.py but
# runtime says ModuleNotFoundError: No module named 'discord'" failure
# mode, which historically meant Render was silently using the Python
# buildpack instead of this Dockerfile. Cheap insurance — the build
# breaks at a grep-able line instead of failing hours later in prod.
RUN python -c "import discord; print('discord.py OK:', discord.__version__)"

# Copy the application source.
COPY . .

# SQLite lives under data/ — the directory must exist in the image so
# Render's persistent-disk mount at runtime can bind over it. We drop
# `chmod 777` because the volume mount overlays the directory at runtime,
# making the in-image chmod irrelevant.
RUN mkdir -p /app/data

CMD ["python", "main.py"]
