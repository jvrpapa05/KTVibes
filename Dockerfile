# KTVibes in one self-contained image.
#
# TORCH_VARIANT picks the PyTorch build:
#   cpu    (default) CPU-only wheels: about 1 GB instead of about 6.5 GB. Use it on hosts with no GPU.
#   cu128  the CUDA 12.8 wheels from uv.lock (needs the NVIDIA container toolkit + `gpus: all`).
ARG PYTHON_VERSION=3.12
ARG NODE_IMAGE=node:22-bookworm-slim

FROM ${NODE_IMAGE} AS node

FROM python:${PYTHON_VERSION}-slim-bookworm
ARG TORCH_VARIANT=cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    KTVIBES_PORT=8765 \
    KTVIBES_CACHE=/data/cache \
    TORCH_HOME=/data/models/torch \
    HF_HOME=/data/models/huggingface \
    XDG_CACHE_HOME=/data/models/xdg

# ffmpeg for audio and video, git for `pip install` of git deps, Node.js 22 for yt-dlp's JS challenges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so code changes don't reinstall PyTorch.
COPY pyproject.toml uv.lock ./
RUN uv venv "$VIRTUAL_ENV" \
    && uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt \
    && if [ "$TORCH_VARIANT" = "cpu" ]; then \
         grep -viE '^(torch|torchaudio|triton|nvidia-)' /tmp/requirements.txt | grep -v '^ *--' > /tmp/req-nontorch.txt \
         && uv pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 torchaudio==2.7.1 \
         && uv pip install -r /tmp/req-nontorch.txt ; \
       else \
         uv pip install -r /tmp/requirements.txt ; \
       fi \
    && rm -f /tmp/req*.txt

COPY . .
RUN uv pip install --no-deps .

# Build-time version stamp (git sha or tag).
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# State lives here and nowhere else: the songs, queue.json and the downloaded models. Migrate this volume.
RUN useradd --system --user-group --uid 10001 --create-home appuser \
    && mkdir -p /data/cache /data/models \
    && chown -R appuser:appuser /data
VOLUME ["/data"]
USER appuser
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
        r=urllib.request.urlopen('http://127.0.0.1:%s/api/config' % os.environ.get('KTVIBES_PORT','8765'), timeout=4); \
        sys.exit(0 if r.status==200 else 1)"

CMD ["ktvibes"]
