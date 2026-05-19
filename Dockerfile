FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir gpxpy pillow watchdog

COPY preprocess.py server.py lumitrail.py viewer.html ./

# /data  → bind mount with your GPX/photos (read-only)
# /output → named volume for thumbnails + SQLite DB
VOLUME ["/data", "/output"]

EXPOSE 8080

ENTRYPOINT ["python", "lumitrail.py"]
CMD ["/data", "-o", "/output", "--watch", "--no-browser"]
