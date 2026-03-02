# Missing .env Credentials

Here’s what’s missing from your `.env` for different modes:

## Paper trading (current setup)

For paper trading you already have what you need:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY`
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`
- `POLYGON_RPC_URL`, `BINANCE_BASE_URL`
- `WALLET_PRIVATE_KEY` — leave empty
- `WALLET_ADDRESS` — placeholder (`0x00...`) is fine

## Live trading

For real-money trading you must add:

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `WALLET_PRIVATE_KEY` | 64-hex private key of your trading wallet | Export from MetaMask/Phantom; use a dedicated wallet only |
| `WALLET_ADDRESS` | Address of that wallet | Same wallet as above |

You also need Polygon USDC in that wallet for placing orders on Polymarket.

## Coinbase AgentKit (gasless USDC on Base)

These are only needed for the AgentKit gasless funding flow on Base:

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `CDP_API_KEY_ID` | Coinbase Developer Platform API key ID | [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/) |
| `CDP_API_KEY_SECRET` | API key secret | Same place |
| `CDP_WALLET_SECRET` | Wallet secret for AgentKit | Same place |

---

**TL;DR**

- Paper trading: your current `.env` is fine.
- Live trading: add `WALLET_PRIVATE_KEY` and `WALLET_ADDRESS` (real values).
- AgentKit: add `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`.
