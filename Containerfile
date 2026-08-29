FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py share_server.py share_store.py user_store.py slide_io.py slide_cache.py locked_atomic_json.py share_entry.sh ./
COPY share_store_json.py user_store_json.py ./
COPY pg_store.py share_store_pg.py user_store_pg.py ./
COPY platform_features.py settings_store.py budget_store.py auth_limit_store.py demo_store.py registration_store.py ./
COPY billing_pricing.py billing_store.py acquisition_store.py ./
COPY crop_guard.py upload_guard.py upload_task_store.py useradmin.py ./
COPY migrations/ migrations/
COPY scripts/ scripts/
COPY docker_entry.sh ./
RUN chmod +x docker_entry.sh
COPY templates/ templates/
COPY static/ static/
COPY plugins/ plugins/

ENV PORT=8000 \
    SHARE_PORT=38000 \
    UPLOAD_DIR=/data/uploads \
    SHARE_DATA_DIR=/data/share \
    PLUGIN_BUNDLES_DIR=/data/plugins

EXPOSE 8000
CMD ["./docker_entry.sh"]
