# PolyQuant

Polymarket BTC 5-min trading bot with paper/live modes, Telegram control panel, and AWAL wallet support.

## Quick Start

1. Copy `.env.example` to `.env` and fill in your values
2. Start the bot: `python app.py`
3. Use the Telegram inline buttons or `/start` for the control panel

## WSL Runtime

The bot runs reliably in **WSL Ubuntu**. The AWAL (Coinbase Agentic Wallet) works there; do not depend on Windows AWAL.

### Setup

1. **Pull latest**
   ```bash
   cd /path/to/polyquant && git pull
   ```

2. **Create `.env`**
   ```bash
   cp .env.example .env
   # Edit .env and paste your keys (NEVER commit .env)
   ```

3. **Start the bot**
   ```bash
   python app.py
   ```
   Or use the management script:
   ```bash
   chmod +x scripts/polyquant
   ./scripts/polyquant up
   ```

4. **Wallet GUI** — open AWAL wallet in WSL:
   ```bash
   awal show
   ```
   Requires WSLg for the GUI. If it doesn't open, run `wsl --update` and ensure WSLg is installed.

5. **Deposit funds** — get your wallet address (via Telegram "Wallet UI" button or `awal address`). Deposit **Base USDC** to that address.

### Management Script

| Command | Description |
|---------|-------------|
| `./scripts/polyquant up` | Start the bot |
| `./scripts/polyquant down` | Stop the bot |
| `./scripts/polyquant restart` | Restart |
| `./scripts/polyquant logs` | Tail runtime logs |
| `./scripts/polyquant status` | Show PID and run state |
| `./scripts/polyquant doctor` | AWAL wallet diagnostics |

## Environment Variables

See `.env.example` for all variables. Minimum for paper mode:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ADMIN_ID` (optional; defaults to chat ID)
- `MODE=paper` (default)

## Telegram Control Panel

Primary interface: **inline buttons** (not slash commands). Send `/start` to open the control panel.

- **Status** — uptime, mode, balance, last error
- **Positions** — paper positions
- **PnL** — performance metrics
- **Providers** — Polymarket CLI + AWAL health
- **WebUI** — dashboard URL
- **Wallet UI** — run `awal show` + instructions
- **Start Paper** / **Pause** — control the trading loop
- **Kill Switch** — emergency stop (admin only)

Non-admin users get read-only access (status, positions, PnL, providers).
