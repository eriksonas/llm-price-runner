FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Data directory for SQLite (writable by the unprivileged user)
RUN mkdir -p /data \
    && useradd --system --uid 1000 --home /app --shell /usr/sbin/nologin pricerunner \
    && chown -R pricerunner:pricerunner /app /data

USER pricerunner

EXPOSE 8080

# --workers 1 is required: APScheduler runs in-process and would duplicate
# refreshes if scaled to multiple worker processes. To handle more load,
# either move scheduling to a sidecar or out-of-process queue.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-server-header"]
