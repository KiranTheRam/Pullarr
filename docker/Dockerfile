# ---- frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
# vite.config outDir points at ../backend/static; keep it inside /build
RUN npm run build -- --outDir /build/static

# ---- backend runtime ----
FROM python:3.12-slim
WORKDIR /app

# Decode RAR/7z to validate downloads without extracting untrusted paths.
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/constraints.txt ./
COPY backend/pullarr ./pullarr
RUN pip install --no-cache-dir -c constraints.txt .

COPY --from=frontend /build/static ./static

ENV PULLARR_DATA_DIR=/config \
    PULLARR_HOST=0.0.0.0 \
    PULLARR_PORT=6997

VOLUME ["/config", "/comics"]
EXPOSE 6997

CMD ["uvicorn", "pullarr.main:app", "--host", "0.0.0.0", "--port", "6997"]
