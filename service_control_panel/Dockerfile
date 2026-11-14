FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN --mount=type=secret,id=build_ca,target=/tmp/build-ca.pem \
    if [ -f /tmp/build-ca.pem ]; then export NODE_EXTRA_CA_CERTS=/tmp/build-ca.pem; fi; npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/src ./backend/src
RUN --mount=type=secret,id=build_ca,target=/tmp/build-ca.pem \
    if [ -f /tmp/build-ca.pem ]; then export PIP_CERT=/tmp/build-ca.pem; fi; \
    pip install --no-cache-dir ./backend && useradd --uid 10001 --create-home panel && mkdir -p /data && chown panel:panel /data
COPY --from=frontend /build/dist ./frontend-dist
ENV PANEL_DATA_DIR=/data PANEL_FRONTEND_DIST=/app/frontend-dist PYTHONUNBUFFERED=1
USER panel
EXPOSE 8000
CMD ["uvicorn", "svc_panel.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
