FROM python:3.12-slim

WORKDIR /app

# System deps (minimal)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY goose.py .
COPY static/ static/

# All runtime data (JSON files, tokens, cached HTML) lives in /data.
# Mount a named volume there so it survives container restarts.
ENV DATA_DIR=/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "goose:app", "--host", "0.0.0.0", "--port", "8080"]
