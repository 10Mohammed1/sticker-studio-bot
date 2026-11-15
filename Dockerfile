FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=1 \
    ORT_NUM_THREADS=1 \
    DATA_DIR=/app/data \
    TEMP_DIR=/app/data/tmp

# FFmpeg is needed for GIF/video/audio and /motion conversions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements*.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install \
        -r requirements.txt \
        -r requirements-pdf.txt \
        -r requirements-vector.txt \
        -r requirements-ai.txt

COPY bot ./bot
COPY migrations ./migrations
COPY VERSION ./

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app
VOLUME ["/app/data"]

CMD ["python", "-m", "bot.main"]
