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
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/constraints.txt ./
COPY backend/pullarr ./pullarr
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -c constraints.txt .

COPY --from=frontend /build/static ./static
COPY docker/healthcheck.py ./healthcheck.py
COPY docker/entrypoint.py ./entrypoint.py
ENTRYPOINT ["python", "-I", "-B", "/app/entrypoint.py"]

ENV PULLARR_DATA_DIR=/config \
    PULLARR_HOST=0.0.0.0 \
    PULLARR_PORT=6997 \
    FORWARDED_ALLOW_IPS=""

VOLUME ["/config", "/comics"]
EXPOSE 6997

# Follow the listener configuration without API keys, proxy routing, or output.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -I -B /app/healthcheck.py >/dev/null 2>&1 || exit 1

# Exec form keeps the server as PID 1 and honors PULLARR_HOST/PULLARR_PORT.
CMD ["python", "-m", "pullarr.main"]
