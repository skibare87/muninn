FROM python:3.12-slim

# hf_xet ships prebuilt wheels; no toolchain needed. curl is here for healthcheck
# and for poking the management API from inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The blob cache. Mount your NVMe array here.
VOLUME ["/cache"]
ENV HF_HUB_CACHE=/cache

# hf_xet's own chunk cache. Worth having on the NAS (it is the one place
# chunk-level dedup across models can actually pay off) but keep it on fast
# local disk, NOT inside /cache -- it is scratch, not content you serve.
ENV HF_XET_CACHE=/xet
VOLUME ["/xet"]

# --- WAN ingest tuning: this is the fast leg, do not cripple it ------------
# Explicitly NOT setting HF_HUB_DISABLE_XET. Xet is what gets you parallel
# range GETs against the CDN instead of one single-threaded stream off the
# LFS bridge. Setting it here is the 3 MB/s failure mode.
ENV HF_XET_NUM_CONCURRENT_RANGE_GETS=32
ENV HF_HUB_ENABLE_HF_TRANSFER=0
# Required by the default XHC_MISS_POLICY=stream, which tail-follows the
# partial file. Verify with scripts/verify_sequential_writes.py after any
# hf_xet upgrade -- sequential reconstruction is not a documented guarantee.
ENV HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-keep-alive", "75", "--no-access-log"]
