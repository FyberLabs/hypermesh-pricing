# Alpine works here: the service's only third-party packages (FastAPI, Pydantic,
# uvicorn) publish musllinux wheels, so the image does not need a compiler.
FROM python:3.13-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRICING_RULESET_DIR=/app/rulesets

RUN adduser -D -u 10001 pricing

COPY pyproject.toml README.md ./
COPY pricing_core ./pricing_core
COPY pricing_service ./pricing_service
COPY rulesets ./rulesets
COPY openapi ./openapi

RUN pip install --no-cache-dir ".[service]" \
    && chown -R pricing:pricing /app

USER pricing
EXPOSE 8080

CMD ["uvicorn", "pricing_service.app:app", "--host", "0.0.0.0", "--port", "8080"]
