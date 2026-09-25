# Alpine works here: the service's only third-party packages (FastAPI, Pydantic,
# uvicorn) publish musllinux wheels, so the image does not need a compiler.
FROM python:3.13-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN adduser -D -u 10001 pricing

COPY pyproject.toml README.md ./
COPY pricing_core ./pricing_core
COPY pricing_service ./pricing_service
COPY openapi ./openapi

RUN pip install --no-cache-dir ".[service]" \
    && chown -R pricing:pricing /app

USER pricing
EXPOSE 8080

# Exits before listening when PRICING_SERVICE_TOKEN is unset or blank.
# The token is read from the environment at start and is never printed.
ENTRYPOINT ["python", "-m", "pricing_service"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]
