# CPU-only, headless Linux amd64 runtime. No registry publication is automatic.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    QT_QPA_PLATFORM=offscreen \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    HOME=/home/vse

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libgl1 libegl1 libglib2.0-0 libdbus-1-3 libxkbcommon0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check

COPY backend ./backend
COPY ui ./ui
COPY gui.py ./
COPY scripts ./scripts
COPY LICENSE ./

ARG VSF_VERSION=6.10
RUN python scripts/install_videosubfinder.py --version "$VSF_VERSION" \
    && python scripts/install_videosubfinder.py --verify-only \
    && python -m backend.tools.model_download --tiers small medium --output backend/models/V6 \
    && python -m backend.cli --help \
    && useradd --create-home --uid 1000 vse \
    && mkdir -p /app/output /app/config /data /home/vse/.cache \
    && chown -R vse:vse /app/output /app/config /home/vse /data

USER vse
ENTRYPOINT ["python", "-m", "backend.cli"]
CMD ["--help"]
