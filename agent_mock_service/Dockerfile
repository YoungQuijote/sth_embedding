FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir '.[api]'
ENV MOCK_DATA_DIR=/data MOCK_LOG_DIR=/logs MOCK_TRACE_DIR=/traces
EXPOSE 32882
CMD ["python", "-m", "uvicorn", "agent_mock_service.app_service:mock_service_app", "--host", "0.0.0.0", "--port", "32882"]
