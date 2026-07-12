FROM python:3.12-slim

# ffmpeg is needed by yt-dlp to merge video+audio / extract mp3
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000
# single worker (downloads run in background threads inside the app), long timeout
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 600 --workers 1 --threads 8
