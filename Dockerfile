# Simple image for the pilot_trader Dash dashboard.
# The app resolves its data paths relative to dashboard.py, so the host project
# dir is bind-mounted over /app at runtime; the COPY below only provides a
# fallback if the volume is absent.
FROM python:3.12-slim

WORKDIR /app

COPY requirements-dashboard.txt ./
RUN pip install --no-cache-dir -r requirements-dashboard.txt

# Bind-mount overlays these at runtime, so the COPYs are a fallback only. The
# Dashboard imports only resolver and the shared account registry; Dash serves
# assets/ (the "ÚJ" badge script) itself.
COPY dashboard.py resolver.py accounts.py evaluation.py signal_semantics.py ./
COPY assets ./assets

ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8051

# Production server instead of Werkzeug's dev server. ONE worker with threads:
# the price/series caches and the cache-warmer thread (PILOT_START_WARMER=1 in
# compose) are per-process, so several workers would each warm and hold their
# own copy. The request line/field limits and the 120 s timeout bound abuse;
# /dev/shm hosts the worker heartbeat file because the root filesystem is
# read-only at runtime.
CMD ["gunicorn", "dashboard:server", "--bind", "0.0.0.0:8051", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "20", "--keep-alive", "5", \
     "--limit-request-line", "4094", "--limit-request-fields", "50", \
     "--limit-request-field_size", "8190", "--worker-tmp-dir", "/dev/shm"]
