FROM python:3.11-slim

WORKDIR /app

# Install system deps (for web3/eth-account native builds)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot/       bot/
COPY config/    config/
COPY control/   control/
COPY data/      data/
COPY execution/ execution/
COPY features/  features/
COPY models/    models/
COPY paper_trading/ paper_trading/
COPY scripts/   scripts/
COPY wallets/   wallets/
COPY wallets_to_watch.txt .

# Persistent data lives outside the image (mounted volume)
VOLUME ["/app/paper_trading"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "bot/main.py"]
