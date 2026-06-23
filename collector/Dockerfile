FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first so the layer caches across source changes.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the source (.dockerignore excludes .venv, tests, caches, secrets).
COPY . .

# Run as an unprivileged user; own the shared log dir so file logging works.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/log/actian \
    && chown -R appuser:appuser /app /var/log/actian
USER appuser

CMD ["python", "-m", "app.main"]
