FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bidpilot ./bidpilot
RUN python -m pip install '.[auth]' \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && useradd --create-home --uid 10001 bidpilot \
    && mkdir -p /app/data /app/outputs/reports \
    && chown -R bidpilot:bidpilot /app

USER bidpilot
EXPOSE 8000
CMD ["python", "-m", "bidpilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
