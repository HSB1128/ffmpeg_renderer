FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app", "--workers=1", "--threads=4", "--timeout=600", "--graceful-timeout=600"]
