FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
ENV PORT=10000 VIDEO_DIR=/app/media OUTPUT_DIR=/app/public
EXPOSE 10000
CMD ["python", "-u", "/app/app.py"]
