FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py share_server.py share_store.py slide_io.py ./
COPY templates/ templates/
COPY static/ static/

ENV PORT=8000 \
    SHARE_PORT=38000 \
    UPLOAD_DIR=/data/uploads \
    SHARE_DATA_DIR=/data/share

EXPOSE 8000

CMD ["python", "app.py"]
