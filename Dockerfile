FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md VERSION ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.txt \
    && uv pip install --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app ./app
RUN chmod -R a+rX /app/app

RUN mkdir -p /app/data /app/temp /data/chzzk_backup

EXPOSE 8733

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8733", "--no-access-log"]
