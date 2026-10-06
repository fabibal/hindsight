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

EXPOSE 8051

CMD ["python", "dashboard.py"]
