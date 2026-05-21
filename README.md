# AI Trading Bot

An autonomous stock analysis and trading system powered by Claude (Anthropic),
TradingAgents (TauricResearch), and TradingView MCP.

> ⚠️ **Disclaimer:** This project is for educational and research purposes.
> Always paper trade before using real capital.
> Nothing here constitutes financial or investment advice.

---

## Architecture

```
Data Layer                        Analysis Layer (7 Agents)                  Execution
──────────                        ─────────────────────────                  ─────────
TradingView MCP (port 9222)   →   Technical Analyst    ┐
Finnhub API                   →   Fundamental Analyst  ├→ Bull/Bear → Risk Mgr → Fund Mgr → MooMoo / Futu
Alpha Vantage                 →   Sentiment Analyst    ┘                                  → Alpaca (paper)
FMP API
NewsAPI
CNN Fear & Greed Index
yfinance (SGX / HK fallback)
```

## Build Status

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Data pipeline (AV + Finnhub + FMP + NewsAPI + Fear & Greed) | ✅ Complete |
| 2 | TradingView MCP setup | ✅ Complete |
| 3 | TradingAgents 7-agent analysis pipeline | ✅ Complete |
| 4 | Risk gate & position sizing | ✅ Complete |
| 5 | Paper trading (Alpaca) + APScheduler + Telegram alerts | ✅ Complete |
| 6 | Live trading (MooMoo/Futu) + Streamlit dashboard | ✅ Complete |

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/jerometan9742/TradingBot.git
cd ai-trading-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
nano .env                       # fill in your API keys

# 3. Create your watchlist
nano watchlist.txt              # one ticker per line (see Watchlist section below)

# 4. Run a single-ticker analysis
python run_analysis.py --ticker AAPL

# 5. Run full watchlist
python run_analysis.py --watchlist

# 6. Launch the Streamlit dashboard
streamlit run monitoring/dashboard.py

# 7. Start the scheduler (runs at 9am + 3pm SGT daily)
python scheduler.py

# 8. Run tests
pytest tests/ -v
```

## Watchlist

Create `watchlist.txt` in the project root — one ticker per line. The scheduler
hot-reloads it on every cycle, so edits take effect without a restart.

```
# US stocks
AAPL
MSFT
GOOGL
NVDA

# SGX stocks — routed via yfinance + MooMoo HK market context
D05.SI    # DBS Group
O39.SI    # OCBC Bank
```

`watchlist.txt` is `.gitignore`d so every developer keeps their own copy.
Falls back to the `WATCHLIST` env variable if the file is not found.

## Brokers

### MooMoo / Futu (Primary)

Uses the **Futu OpenAPI** (`futu-api`) to trade US, SGX, and HK stocks from a
single account.

**Setup:**
1. Download and install **FutuOpenD** gateway: [futunn.com/download/OpenAPI](https://www.futunn.com/download/OpenAPI)
2. Start FutuOpenD — it listens on `127.0.0.1:11111` by default
3. Unlock trading inside FutuOpenD (phone 2FA) once per session
4. Set `TRADING_MODE=paper` (simulate) or `TRADING_MODE=live` in `.env`

Ticker routing is automatic:
- `AAPL` → US market
- `D05.SI`, `0700.HK` → MooMoo HK market context (Futu routes SGX this way)

### Alpaca (Paper Trading Fallback)

Set `ALPACA_BASE_URL=https://paper-api.alpaca.markets` for commission-free
paper trading on US stocks. No local gateway required.

## Project Structure

```
ai-trading-bot/
├── data/
│   ├── fetcher.py              # Unified DataFetcher (all sources)
│   ├── alpha_vantage.py        # Technical indicators + OHLCV history
│   ├── finnhub.py              # Real-time quotes, news, earnings calendar
│   ├── fmp.py                  # Fundamentals, ratios, DCF
│   ├── yfinance_connector.py   # SGX / HK / international fallback (free)
│   ├── newsapi.py              # News headlines via NewsAPI
│   └── fear_greed.py           # CNN Fear & Greed Index (free, no key)
├── charts/
│   └── chart_reader.py         # TradingView MCP wrapper
├── agents/
│   ├── trading_agents.py       # 7-agent pipeline
│   └── skills/                 # Agent skill prompt files
├── risk/
│   ├── risk_gate.py            # Trade filter + kill switch
│   └── position_sizer.py       # Fixed-fractional sizing
├── execution/
│   ├── moomoo.py               # MooMoo / Futu (US + SGX + HK)
│   ├── alpaca.py               # Alpaca paper + live (US only)
│   └── tiger.py                # Tiger Brokers connector
├── monitoring/
│   ├── logger.py               # JSON decision log + trades CSV
│   ├── telegram_alerts.py      # Telegram notifications
│   ├── watchlist.py            # Hot-reloading watchlist manager
│   └── dashboard.py            # Streamlit dashboard (localhost:8501)
├── backtest/
│   └── nautilus_runner.py      # NautilusTrader backtesting
├── tests/
├── scheduler.py                # APScheduler — 9am + 3pm SGT
├── run_analysis.py             # CLI entry point
└── watchlist.txt               # Your tickers (gitignored, create locally)
```

## VPS Deployment (Hetzner CX23)

Recommended server: **Hetzner CX23** — 2 vCPU ARM, 4 GB RAM, 40 GB SSD, ~€3.79/mo.

### 1 — Provision

```bash
# Install Hetzner CLI
brew install hcloud

hcloud server create \
  --name trading-bot \
  --type cx23 \
  --image ubuntu-24.04 \
  --location sin          # Singapore datacenter
```

### 2 — Initial server setup

```bash
# SSH in as root
ssh root@<SERVER_IP>

# Create a non-root user
adduser trader && usermod -aG sudo trader
su - trader

# Install Python 3.11+
sudo apt update && sudo apt install -y python3.11 python3.11-venv git

# Clone repo
git clone https://github.com/jerometan9742/TradingBot.git ~/ai-trading-bot
cd ~/ai-trading-bot

# Virtualenv + dependencies
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Environment & watchlist
cp .env.example .env
nano .env               # fill in all API keys
nano watchlist.txt      # add your tickers
```

### 3 — systemd service

Create `/etc/systemd/system/trading-bot.service`:

```ini
[Unit]
Description=AI Trading Bot Scheduler
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/ai-trading-bot
EnvironmentFile=/home/trader/ai-trading-bot/.env
ExecStart=/home/trader/ai-trading-bot/venv/bin/python scheduler.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot

# Tail logs
journalctl -u trading-bot -f
```

### 4 — Dashboard access (SSH tunnel)

The Streamlit dashboard binds to `localhost:8501` inside the VPS. Forward it
locally via an SSH tunnel — no need to open the port publicly:

```bash
# On your local machine
ssh -L 8501:localhost:8501 trader@<SERVER_IP>

# Then open in browser
open http://localhost:8501
```

> If you need public access, restrict with `ufw allow from <YOUR_IP> to any port 8501` before binding to `0.0.0.0`.

### 5 — SSH key setup (recommended)

```bash
# Generate a key pair locally (if you don't have one)
ssh-keygen -t ed25519 -C "trading-bot-vps"

# Copy to server
ssh-copy-id -i ~/.ssh/id_ed25519.pub trader@<SERVER_IP>

# Disable password auth on the server
sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart sshd
```

## Environment Variables

See `.env.example` for the full list. Key settings:

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6

ALPHA_VANTAGE_API_KEY=...
FINNHUB_API_KEY=...
FMP_API_KEY=...              # upgrade to $19/mo for fundamentals
NEWSAPI_KEY=...

MOOMOO_HOST=127.0.0.1        # FutuOpenD gateway (local or VPS)
MOOMOO_PORT=11111
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

TRADING_MODE=paper           # change to 'live' only after validation
MAX_POSITION_SIZE_PCT=0.05
MAX_DAILY_LOSS_PCT=0.02
MIN_CONFIDENCE_SCORE=7
MAX_TRADES_PER_DAY=5
```

## Monthly Running Costs

| Item | Cost |
|------|------|
| Claude API (with prompt caching ~90% hit rate) | ~$5–15/mo |
| Alpha Vantage free tier | $0 |
| Finnhub free tier | $0 |
| FMP Starter (required for fundamentals) | $19/mo |
| NewsAPI free tier | $0 |
| CNN Fear & Greed (no account needed) | $0 |
| Hetzner CX23 VPS | ~$5/mo |
| **Total (paper trading)** | **~$29–39/mo** |

Key optimisation: prompt caching with a 1-hour TTL reduces Claude API costs by
~90% on repeat scheduler runs.

## Tech Stack

- **Claude Sonnet 4.6** — LLM backbone for all 7 agents
- **TradingAgents** — multi-agent bull/bear debate framework
- **TradingView MCP** — live chart data via Chrome DevTools Protocol
- **Alpha Vantage** — OHLCV + 50+ technical indicators
- **Finnhub** — real-time quotes, news, earnings calendar
- **FMP** — fundamentals, ratios, DCF valuation
- **yfinance** — SGX / HK / international fallback (free, no key required)
- **NewsAPI** — news headlines
- **CNN Fear & Greed** — market sentiment index (free)
- **MooMoo / Futu** — primary broker (US + SGX + HK via FutuOpenD)
- **Alpaca** — paper trading fallback (US stocks, commission-free)
- **APScheduler** — 9am + 3pm SGT daily runs
- **Streamlit + Plotly** — live dashboard at `localhost:8501`

---

*Built with [Claude](https://anthropic.com) · [TradingAgents](https://github.com/TauricResearch/TradingAgents) · [TradingView MCP](https://github.com/tradesdontlie/tradingview-mcp)*
