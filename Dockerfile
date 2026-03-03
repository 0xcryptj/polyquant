# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# gcc/g++ needed for web3, eth-account, and some ccxt native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Copy compiled packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Non-root user for security (uid 1001 avoids collision with host UID 1000)
RUN useradd --uid 1001 --no-create-home --shell /bin/false polyquant \
    && mkdir -p /app/paper_trading /app/logs \
    && chown -R polyquant:polyquant /app

# Copy application source
COPY --chown=polyquant:polyquant app.py               ./
COPY --chown=polyquant:polyquant bot/                 bot/
COPY --chown=polyquant:polyquant config/              config/
COPY --chown=polyquant:polyquant control/             control/
COPY --chown=polyquant:polyquant data/                data/
COPY --chown=polyquant:polyquant execution/           execution/
COPY --chown=polyquant:polyquant features/            features/
COPY --chown=polyquant:polyquant models/              models/
COPY --chown=polyquant:polyquant paper_trading/       paper_trading/
COPY --chown=polyquant:polyquant runtime/             runtime/
COPY --chown=polyquant:polyquant scripts/             scripts/
COPY --chown=polyquant:polyquant services/            services/
COPY --chown=polyquant:polyquant wallets/             wallets/
COPY --chown=polyquant:polyquant web/                 web/
COPY --chown=polyquant:polyquant wallets_to_watch.txt ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Persistent data lives outside the image (mounted volume)
VOLUME ["/app/paper_trading", "/app/logs"]

USER polyquant

# Healthcheck: verify DB is readable (fast, no network)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "from paper_trading import persistence as db; db.init_db(); print('ok')" || exit 1

CMD ["python", "app.py"]
