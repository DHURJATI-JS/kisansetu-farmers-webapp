# KisanSetu production image (non-root, migrations on start, OCR engines included)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENV=production AUTO_CREATE_TABLES=false REDIS_REQUIRED=true HOST=0.0.0.0 PORT=8088

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin libglib2.0-0 libgl1 libgomp1 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 kisan

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt pytailwindcss

COPY --chown=kisan:kisan . .
# self-host vendor JS + face model, then build minified/hashed assets (+SRI) and Tailwind CSS
RUN python scripts/fetch_vendor.py && python scripts/build_assets.py && mkdir -p var && chown -R kisan:kisan /app

USER kisan
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://localhost:8088/healthz || exit 1
CMD ["sh", "-c", "alembic upgrade head && python run.py"]
