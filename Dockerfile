# CPU-only, headless Linux amd64 runtime. No registry publication is automatic.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    QT_QPA_PLATFORM=offscreen \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    HOME=/home/vse

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libgl1 libegl1 libglib2.0-0 libdbus-1-3 libxkbcommon0 libgomp1 \
       libfreetype6 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Build dependencies and wheel caches never enter the final runtime layers.
FROM base AS dependencies
WORKDIR /build
COPY requirements.txt requirements-inference.txt ./
COPY scripts/runtime_dependencies.py scripts/prune_inference_runtime.py ./scripts/
RUN python -m pip install --no-cache-dir packaging \
    && python -m pip wheel --no-cache-dir -r requirements.txt -r requirements-inference.txt --wheel-dir /wheels \
    && python scripts/runtime_dependencies.py --wheelhouse /wheels --requirements requirements-inference.txt --output /runtime-wheels \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --no-deps --no-compile /runtime-wheels/*.whl \
    && /opt/venv/bin/python scripts/prune_inference_runtime.py --apply --include-unused-openvino --manifest /opt/venv/.inference-prune.json \
    && /opt/venv/bin/python scripts/runtime_dependencies.py --manifest /runtime-wheels/runtime-manifest.json

FROM base AS runtime
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=dependencies /runtime-wheels/runtime-manifest.json /app/runtime-manifest.json

COPY backend ./backend
COPY ui ./ui
COPY gui.py ./
COPY scripts ./scripts
COPY LICENSE ./

ARG VSF_VERSION=6.10.5
RUN python scripts/install_videosubfinder.py --version "$VSF_VERSION" \
    && python scripts/install_videosubfinder.py --verify-only \
    && python -m backend.tools.model_download --tiers small medium --output backend/models/V6 \
    && python -m backend.cli --help \
    && useradd --create-home --uid 1000 vse \
    && mkdir -p /app/output /app/config /data /home/vse/.cache \
    && chown -R vse:vse /app/output /app/config /home/vse /data

# Import and exercise the heavy native runtime as the actual unprivileged user.
# CLI --help/--version intentionally skip these imports and cannot catch missing
# Qt/Paddle shared libraries (for example FreeType/fontconfig).
USER vse
RUN python scripts/prune_inference_runtime.py --verify --manifest /opt/venv/.inference-prune.json \
    && python scripts/runtime_dependencies.py --manifest runtime-manifest.json \
    && python scripts/check_runtime.py
ENTRYPOINT ["python", "-m", "backend.cli"]
CMD ["--help"]
