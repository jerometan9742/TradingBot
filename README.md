# Fred — AI Swing Trading Bot

> Daily swing trading signal generator powered by Claude AI. Uses a 7-agent multi-agent debate pipeline to analyse US stocks, then routes decisions through a hard-guardrail risk gate before placing bracket orders via MooMoo/Futu.

> **For educational and research purposes only. Not financial advice. Always paper trade before using real capital.**

---

## What Fred Does

Fred runs on a schedule on a VPS, analysing a configurable watchlist of US stocks:

1. **Fetches live data** — real-time quotes, fundamentals, technicals, news, and sentiment from 5 data sources
2. **Runs a 7-agent debate pipeline** — bull and bear researchers argue their cases; a Fund Manager decides BUY / HOLD / SELL with a confidence score (1–10)
3. **Passes through a Risk Gate** — 7 hard guardrails block trades that don't meet criteria
4. **Sizes positions** — fixed-fractional sizing scaled by confidence tier
5. **Places bracket orders** — entry + stop-loss + take-profit submitted to MooMoo via FutuOpenD
6. **Monitors 24/7** — a background service polls open positions every 60 seconds as a backup SL/TP enforcer
7. **Alerts via Telegram** — every trade placed, blocked, or filled; daily P&L summary
8. **Learns from trades** — post-trade Claude reflections update `lessons_learned.md`, injected into future sessions

---

## Architecture

```
DATA LAYER
──────────
Alpha Vantage API       →  RSI, MACD, Bollinger Bands, ATR, ADX, OHLCV
Finnhub API             →  Real-time quotes, news, earnings calendar, analyst targets
FMP API                 →  Fundamentals, key ratios, earnings surprises
yfinance                →  International tickers (.SI/.HK/.L etc), gap-fill for US
NewsAPI                 →  Additional news articles (100 req/day free)
CNN Fear & Greed Index  →  Market sentiment (free, no key required)

ANALYSIS LAYER  (agents/trading_agents.py)
──────────────────────────────────────────
[Technical Analyst]    →  RSI, MACD, ADX, ATR, Bollinger Bands, trend direction
[Fundamental Analyst]  →  P/E, revenue, margins, earnings quality, valuation
[Sentiment Analyst]    →  News tone, Fear & Greed index, crowd sentiment
[Bull Researcher]      →  Strongest bull case from all available data
[Bear Researcher]      →  Strongest bear case from all available data
[Risk Manager]         →  Risk/reward assessment; may veto or downgrade
[Fund Manager]         →  Final BUY / HOLD / SELL + confidence score (1–10)

RISK LAYER  (risk/)
────────────────────
RiskGate        →  7-check hard guardrail (confidence, kill switch, daily limits, flags)
PositionSizer   →  Fixed-fractional sizing scaled by confidence tier; ATR-based SL/TP

EXECUTION LAYER  (execution/)
──────────────────────────────
moomoo.py  →  Paper and live trading via FutuOpenD (US, HK, SGX)

MONITORING LAYER  (monitoring/)
────────────────────────────────
dashboard.py        →  6-page Streamlit dashboard (port 8502)
telegram_alerts.py  →  Trade alerts, daily summaries, Telegram command handler
price_monitor.py    →  24/7 backup SL/TP watcher (polls every 60s)
logger.py           →  Structured JSON session logs + trades.csv

MEMORY LAYER  (agents/memory/)
───────────────────────────────
reflection_engine.py  →  Post-trade Claude reflections, weekly learning summary
lessons_learned.md    →  Accumulated trading lessons injected into each session
```

---

## Trading Strategy (V2 — ADX Fixed)

The V2 baseline strategy was derived from backtesting 7 strategy combinations across META, NVDA, AAPL, and MSFT on 2 years of daily data.

| Parameter | Value | Notes |
|-----------|-------|-------|
| Timeframe | Daily | Swing trading; holds 1–5 days |
| ADX filter | > 15 | Trending market filter (now properly fetched) |
| RSI entry | > 40 | Momentum confirmation |
| MACD | Line > signal AND > 0 | Trend direction |
| Volume | > 20-bar SMA | Confirms participation |
| Stop Loss | ATR(14) × 1.5 | Dynamic, volatility-adjusted |
| Take Profit | ATR(14) × 3.0 | 2:1 risk/reward ratio |
| EMA filter | None | Removed — too restrictive on daily |
| BB filter | None | Removed — was blocking all trades |

**Backtested results (V2 with ADX fix):**
- META: +7.39% P&L, 2.46 P&L/DD ratio
- NVDA: +18.35% P&L (ADX fix prevented range-bound entries)
- Win rate: ~41–44%

**ADX bug fix (May 2025):** ADX > 15 was in agent system prompts but was never fetched from any data source — the bot had no ADX value to evaluate. Fixed by adding `AlphaVantageClient.get_adx()` and wiring `adx_14` into `DataFetcher._fetch_technicals()`.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| AI / Agents | Claude claude-sonnet-4-6 (Anthropic API) |
| Broker | MooMoo / Futu via `futu-api` SDK |
| Scheduler | APScheduler (BlockingScheduler + CronTrigger) |
| Dashboard | Streamlit + Plotly |
| Data (US technical) | Alpha Vantage |
| Data (US quote/news) | Finnhub |
| Data (US fundamentals) | FMP (Financial Modeling Prep) |
| Data (intl/gap-fill) | yfinance |
| Alerts | Telegram Bot API |
| Memory | Claude-generated JSON reflections |
| Deployment | VPS (Ubuntu 26.04) + GitHub auto-deploy |

---

## Infrastructure

### VPS
- **Host**: 46.62.165.36 (Hetzner CX21, Ubuntu 26.04)
- **Dashboard**: `http://46.62.165.36:8502`
- **Auto-deploy**: VPS polls GitHub every 5 minutes, restarts services, and sends a Telegram notification on each deploy

### Systemd services
| Service | Purpose |
|---------|---------|
| `trading-bot` | Main scheduler (scheduler.py) |
| `price-monitor` | 24/7 SL/TP watcher (monitoring/price_monitor.py) |
| `dashboard` | Streamlit dashboard on port 8502 |

### FutuOpenD
MooMoo/Futu requires a locally-running FutuOpenD daemon. The bot connects to `127.0.0.1:11111` by default.

- `TRADING_MODE=paper` → `TrdEnv.SIMULATE` (paper account)
- `TRADING_MODE=live` → `TrdEnv.REAL` (real account — use with caution)

### Kill switch
Creating `kill_switch.lock` in the project root immediately halts all trading. Remove the file to resume. Accessible from the Telegram `/killswitch` command or the dashboard Settings page.

---

## Agent Pipeline

Each ticker passes through all 7 agents sequentially. Agents share a conversation thread so later agents can reference earlier analysis.

### 1. Technical Analyst
Reads: RSI-14, MACD, ADX-14, ATR-14, Bollinger Bands, price action.
Produces: trend direction, momentum assessment, key support/resistance levels.

### 2. Fundamental Analyst
Reads: P/E, P/B, EV/EBITDA, ROE, debt/equity, revenue, net income, margins, earnings surprises, analyst targets.
Produces: valuation assessment, earnings quality, balance sheet health.

### 3. Sentiment Analyst
Reads: Finnhub news sentiment, recent headlines (up to 15 articles), CNN Fear & Greed Index.
Produces: news tone, market sentiment, macro backdrop.

### 4. Bull Researcher
Synthesises the strongest possible bull case from all data. No veto power.

### 5. Bear Researcher
Synthesises the strongest possible bear case from all data. No veto power.

### 6. Risk Manager
Evaluates risk/reward. Can downgrade a BUY to HOLD or issue a veto. Flags critical risks (halt, delist, fraud, bankruptcy, SEC investigation).

### 7. Fund Manager
Makes the final call: **BUY / HOLD / SELL** + confidence score 1.0–10.0.
- Confidence ≥ 7.0 required for BUY or SELL
- Python safety guard: any BUY/SELL with confidence < 7.0 is automatically downgraded to HOLD
- Injects `lessons_learned.md` from past trades into this agent's context

**Output schema:**
```json
{
  "ticker":      "AAPL",
  "action":      "BUY",
  "confidence":  8.2,
  "reasoning":   "...",
  "bull_case":   "...",
  "bear_case":   "...",
  "risk_flags":  [],
  "analysed_at": "2025-05-22T13:00:00"
}
```

### Prompt caching
All 7 system prompts use `cache_control: {"type": "ephemeral", "ttl": "1h"}`. Because the scheduler runs every ~90 minutes, this saves ~90% of Claude API costs — only the per-ticker market data (user messages) is billed at full price.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/help` | List all available commands |
| `/status` | Bot status — last run, next run, kill switch state, trading mode |
| `/signals` | Latest BUY/SELL signals from the most recent session |
| `/positions` | Open positions with entry price, current price, and unrealised P&L |
| `/history` | Last 10 closed trades with P&L |
| `/stats` | Win rate, total P&L, R/R ratio, streaks, Sharpe ratio |
| `/news` | Latest news headlines for watchlist tickers |
| `/chart` | TradingView chart screenshot for a ticker (e.g. `/chart AAPL`) |
| `/summary` | Daily P&L summary |
| `/screenstock` | Run the screener and return top 15 candidates |
| `/pause` | Activate kill switch (halts trading) |
| `/resume` | Deactivate kill switch (resumes trading) |
| `/killswitch` | Toggle kill switch on/off |

---

## Schedule (SGT, Mon–Fri)

| Time (SGT) | Job | Confidence threshold |
|------------|-----|---------------------|
| 6:00 PM | Universe auto-update (watchlist_universe.txt refresh) | — |
| 7:00 PM | Screener runs; Telegram top 15 candidates | — |
| 8:00 PM | Pre-market analysis | ≥ 8.5 (higher bar pre-open) |
| 9:30 PM | NYSE open analysis | ≥ 7.0 |
| 3:00 AM | Afternoon momentum check | ≥ 7.0 |
| 4:00 AM | Market close — P&L summary, reflection trigger | — |
| **Sunday 9:00 AM** | Weekly learning summary | — |

---

## Risk Management

### Risk Gate (risk/risk_gate.py)

7 hard checks run in order. Any failure blocks the trade immediately.

| Check | Condition |
|-------|-----------|
| 1. Kill switch | `kill_switch.lock` must not exist |
| 2. Confidence | `decision.confidence >= MIN_CONFIDENCE_SCORE` (default 7.0) |
| 3. Action validity | Action must be BUY or SELL (not HOLD) |
| 4. Daily trade limit | `trades_today < MAX_TRADES_PER_DAY` (default 5) |
| 5. Daily loss limit | `daily_pnl > -MAX_DAILY_LOSS_PCT` (default –2%); auto-activates kill switch if breached |
| 6. Critical risk flags | No halt / delist / fraud / bankruptcy / SEC investigation flags |
| 7. Valid price | Quote price must be > 0 |

### Position Sizing (risk/position_sizer.py)

Equity is always fetched live from MooMoo — never hardcoded.

| Confidence | Position size |
|-----------|--------------|
| ≥ 9.0 | 100% of max (default 5% of equity) |
| ≥ 8.0 | 75% of max |
| ≥ 7.0 | 50% of max |

- **Stop loss**: `entry_price - (ATR × 1.5)`
- **Take profit**: `entry_price + (ATR × 3.0)`
- **Minimum trade value**: $100 USD
- **Fallback** (no ATR): 3% SL, 6% TP (2:1 R/R)

---

## Streamlit Dashboard

6-page dashboard at `http://46.62.165.36:8502` (or `localhost:8502` locally).

| Page | Contents |
|------|---------|
| 📊 Live Overview | Account equity, cash, daily P&L, unrealised P&L; bot status (last run, next run, kill switch, mode); latest watchlist signals |
| 📋 Open Positions | Live positions with entry/current price, quantity, value, unrealised P&L, SL/TP levels, days held; one-click close button |
| 📜 Trade History | Trade log, cumulative P&L chart, win rate, avg win/loss, profit factor, Sharpe ratio, best/worst trades |
| 🤖 Bot Decisions | Session browser — full agent reasoning per ticker (technical, fundamental, sentiment, bull/bear case, risk gate result, position sizing, news) |
| 📈 Performance | Equity curve vs SPY benchmark, drawdown chart, monthly returns heatmap, P&L by ticker bar chart, confidence vs outcome scatter plot |
| ⚙️ Settings | Kill switch toggle, watchlist editor, risk parameter editor, manual analysis trigger, API status panel |

---

## Running Costs

| Item | Cost / month |
|------|-------------|
| Claude API (with prompt caching) | ~$5–15 |
| Alpha Vantage (free tier) | $0 |
| Finnhub (free tier) | $0 |
| FMP (free tier — ⚠️ fundamentals blocked) | $0 |
| FMP Starter (needed for full signals) | $19 |
| VPS (Hetzner CX21) | ~$6 |
| TradingView Essential | ~$13 |
| **Current total (paper, free APIs)** | **~$5–15/mo** |
| **Full paper trading total** | **~$24–34/mo** |

---

## Key Decisions & Why

| Decision | Reason |
|----------|--------|
| MooMoo/Futu instead of Alpaca | User already has Futu app; supports SGX/HK markets |
| Daily timeframe | LLMs are too slow for intraday; daily gives time to reason |
| EMA200 filter removed | Was too restrictive — blocked most valid setups on daily |
| Bollinger Band filter removed | Was blocking all trades — price rarely touches bands on daily |
| ADX threshold lowered 20 → 15 | Improved trade frequency without sacrificing trend quality |
| RSI threshold lowered 50 → 40 | Better entry timing — catches momentum earlier |
| Fixed 3.0x TP (not dynamic) | Backtesting showed fixed outperforms dynamic TP tiers |
| FVG filter not used | Too few trades (avg 5 vs 21 without) on daily timeframe |
| ATR 1.5x SL / 3.0x TP | Backtested as best risk/reward combination across 4 tickers |
| Dashboard shows USD only | User preference; HKD conversion adds complexity for US stocks |
| Prompt caching TTL = 1 hour | Scheduler runs ~90 min cycles; 5-min default TTL would always expire |

---

## Known Issues & Watchlist

| Issue | Impact | Fix |
|-------|--------|-----|
| FMP free tier blocks fundamentals (403) | Confidence scores capped ~5.5 → most trades are HOLDs | Upgrade FMP to $19/mo Starter plan |
| Alpha Vantage 25 req/day limit | Can only fully analyse ~3 tickers/day on free tier | Upgrade or cache aggressively |
| Finnhub sentiment blocked on free tier | No buzz/sentiment scores | Use news headlines as proxy (working) |
| SGX tickers (DBS.SI etc) have limited data | Singapore stocks get less complete analysis | yfinance fallback partially handles this |

---

## Future Plans

- **Phase 5**: Upgrade FMP to Starter ($19/mo) — unlocks fundamentals, enables real BUY signals
- **Phase 5**: Telegram command improvements (inline keyboards, position management)
- **Phase 6**: SGX and HK live trading via Tiger Brokers (`execution/tiger.py`)
- **Phase 6**: TradingView MCP integration for live chart signals alongside API data
- **Phase 6**: NautilusTrader backtesting pipeline (`backtest/nautilus_runner.py`)
- **Ongoing**: Accumulate reflections in `lessons_learned.md` to improve Fund Manager accuracy over time

---

## Setup Guide

### Prerequisites

- Python 3.11+
- FutuOpenD installed and running (download from Futu/MooMoo desktop app)
- Telegram bot created via @BotFather

### 1. Clone and install

```bash
git clone https://github.com/jerometan9742/TradingBot.git
cd TradingBot
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required `.env` keys:

```env
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6

# Financial data
ALPHA_VANTAGE_API_KEY=...
FINNHUB_API_KEY=...
FMP_API_KEY=...
NEWSAPI_KEY=...                    # optional

# Broker (MooMoo)
BROKER=moomoo
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
TRADING_MODE=paper                 # paper or live

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Bot config
WATCHLIST=AAPL,MSFT,NVDA,META
MAX_POSITION_SIZE_PCT=0.05         # 5% max per position
MAX_DAILY_LOSS_PCT=0.02            # 2% daily loss kill switch
MIN_CONFIDENCE_SCORE=7.0
MAX_TRADES_PER_DAY=5
```

### 3. Start FutuOpenD

Open the Futu/MooMoo desktop app and ensure FutuOpenD is running on port 11111. For paper trading, select the Simulate account.

### 4. Run

```bash
# Activate virtualenv
source venv/bin/activate

# Single ticker analysis (no order placement)
python run_analysis.py --ticker AAPL

# Full watchlist analysis
python run_analysis.py --watchlist

# Full watchlist + place approved orders
python run_analysis.py --watchlist --execute

# Start the dashboard
streamlit run monitoring/dashboard.py

# Start the full scheduler (runs on the cron schedule above)
python scheduler.py

# Run tests
pytest tests/ -v

# Activate kill switch manually
touch kill_switch.lock

# Deactivate kill switch
rm kill_switch.lock
```

### 5. Deploy to VPS

The VPS auto-deploys from GitHub every 5 minutes. Push to `main` to trigger a deploy.

```bash
git push origin main
# VPS will pull and restart services automatically within 5 minutes
# Telegram notification sent on each deploy
```

---

## Project Structure

```
ai-trading-bot/
├── CLAUDE.md                     ← Claude Code session reference
├── README.md                     ← This file
├── .env                          ← API keys (never commit)
├── .env.example                  ← Template (safe to commit)
├── requirements.txt
├── run_analysis.py               ← Main entry point
├── scheduler.py                  ← APScheduler cron runner
├── watchlist.txt                 ← Active trading tickers
├── watchlist_universe.txt        ← Screener universe
│
├── data/
│   ├── fetcher.py                ← Unified DataFetcher (all sources → one dict)
│   ├── alpha_vantage.py          ← RSI, MACD, BB, ATR, ADX, OHLCV
│   ├── finnhub.py                ← Real-time quotes, news, earnings, analyst
│   ├── fmp.py                    ← Fundamentals, ratios, earnings surprises
│   ├── yfinance_connector.py     ← International tickers + gap-fill
│   ├── fear_greed.py             ← CNN Fear & Greed Index
│   ├── newsapi.py                ← NewsAPI connector
│   ├── screener.py               ← Daily universe screener
│   └── universe_builder.py       ← Auto-builds watchlist universe
│
├── agents/
│   ├── trading_agents.py         ← 7-agent pipeline
│   ├── skills/                   ← Agent skill prompts
│   └── memory/
│       ├── reflection_engine.py  ← Post-trade learning pipeline
│       ├── lessons_learned.md    ← Accumulated trading lessons
│       └── reflections/          ← Per-trade JSON reflection files
│
├── risk/
│   ├── risk_gate.py              ← 7-check trade filter + kill switch
│   └── position_sizer.py         ← Fixed-fractional ATR-based sizing
│
├── execution/
│   └── moomoo.py                 ← MooMoo/Futu connector (paper + live)
│
├── monitoring/
│   ├── dashboard.py              ← Streamlit dashboard (6 pages)
│   ├── telegram_alerts.py        ← Alert methods + Telegram command handler
│   ├── telegram_listener.py      ← Command polling thread
│   ├── price_monitor.py          ← 24/7 backup SL/TP watcher
│   └── logger.py                 ← JSON session logs + trades.csv
│
├── backtest/
│   └── nautilus_runner.py        ← NautilusTrader backtesting (planned)
│
├── logs/                         ← Session JSON files + trades.csv (gitignored)
└── tests/
    ├── test_data_fetcher.py
    └── test_risk_gate.py
```

---

*Owner: jerometan9742 | Repo: github.com/jerometan9742/TradingBot*
