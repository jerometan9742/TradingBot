# 🤖 AI Trading Bot — Claude Code Reference

> This file is read automatically by Claude Code on every session.
> It contains everything needed to understand the project, current status,
> coding conventions, and what to build next.

---

## claude-mem Memory Guidance

Key things to always remember across sessions:
- This is an AI trading bot using MooMoo/Futu paper trading via FutuOpenD on VPS at 46.62.165.36
- Broker: BROKER=moomoo in .env
- Dashboard runs at http://46.62.165.36:8502
- Auto-deploy: VPS polls GitHub every 5 mins, restarts services, sends Telegram notification
- All Telegram commands are in monitoring/telegram_alerts.py
- Trade reflections stored in agents/memory/
- Strategy: Daily timeframe, ADX>15, RSI>40, ATR 1.5x SL / 3.0x TP (V2 baseline)
- Paper trading $1M USD on MooMoo simulate account
- Do NOT use Alpaca — broker is MooMoo only
- Do NOT hardcode PAPER_EQUITY — fetch live from MooMooConnector().get_account_balance()["by_market"]["US"]["total_assets"]
- Price monitor runs 24/7 as systemd service
- Scheduler runs at 8pm, 9:30pm, 3am, 4am SGT

---

## 📌 What This Bot Does

A **daily swing trading signal generator** that:
1. Reads live charts from TradingView Desktop via MCP server
2. Fetches fundamentals, news, sentiment from financial APIs
3. Runs a 7-agent multi-agent analysis pipeline (bull vs bear debate)
4. Passes decisions through a Risk Gate with hard guardrails
5. Sizes positions using fixed-fractional position sizing
6. Places trades via brokerage API (Alpaca paper → Tiger Brokers live)
7. Alerts via Telegram and displays everything on a Streamlit dashboard
8. Runs twice daily: 9am SGT (market open) and 3pm SGT (pre-close)

> ⚠️ For educational and research purposes. Not financial advice.
> Always paper trade before using real capital.

---

## ✅ Build Status

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Data pipeline (Alpha Vantage + Finnhub + FMP) | ✅ Complete |
| Phase 2 | TradingView MCP connected to Claude Code | ✅ Complete |
| Phase 3 | 7-agent TradingAgents pipeline + run_analysis.py | ✅ Complete |
| Phase 4 | Risk Gate + Position Sizer | 🟡 In Progress |
| Phase 5 | Paper trading (Alpaca) + scheduler + Telegram alerts | ⬜ Next |
| Phase 6 | Live trading + Streamlit dashboard + VPS deployment | ⬜ Planned |

---

## 🏗️ Architecture

```
DATA LAYER
──────────
TradingView MCP (port 9222)  →  Technical signals (OHLCV, RSI, MACD, BB)
Alpha Vantage API            →  Technical indicators, OHLCV history
Finnhub API                  →  Real-time quotes, news, earnings calendar
FMP API                      →  Fundamentals, ratios, earnings, DCF
yfinance (planned)           →  SGX stocks fallback (DBS.SI, OCBC etc)

ANALYSIS LAYER (agents/trading_agents.py)
─────────────────────────────────────────
[Technical Analyst]    reads: quote + technicals
[Fundamental Analyst]  reads: fundamentals + earnings + analyst
[Sentiment Analyst]    reads: sentiment + news
        ↓
[Bull Researcher]  ←→  [Bear Researcher]  (debate)
        ↓
[Risk Manager]     (veto check)
        ↓
[Fund Manager]     → { action, confidence, reasoning, risk_flags }

RISK LAYER (risk/)
──────────────────
RiskGate        → blocks trade if confidence < 7, kill switch, daily limits
PositionSizer   → fixed-fractional sizing, scales with confidence score

EXECUTION LAYER (execution/)
─────────────────────────────
alpaca.py   → paper trading (Phase 5) → live US stocks (Phase 6)
tiger.py    → SGX / HK / US live trading (Phase 6)

MONITORING LAYER (monitoring/)
───────────────────────────────
logger.py           → structured JSON decision logs + trades CSV
telegram_alerts.py  → trade placed, blocked, daily P&L summary
dashboard.py        → Streamlit dashboard (Phase 6)
```

---

## 📁 Project Structure

```
ai-trading-bot/
├── CLAUDE.md                  ← YOU ARE HERE
├── README.md
├── .env                       ← API keys (never commit)
├── .env.example               ← Template (safe to commit)
├── .gitignore
├── requirements.txt
├── docker-compose.yml
├── run_analysis.py            ← Main entry point
│
├── data/
│   ├── fetcher.py             ← Unified DataFetcher class
│   ├── alpha_vantage.py       ← Alpha Vantage connector
│   ├── finnhub.py             ← Finnhub connector
│   └── fmp.py                 ← FMP connector
│
├── charts/
│   └── chart_reader.py        ← TradingView MCP wrapper
│
├── agents/
│   ├── trading_agents.py      ← 7-agent pipeline (COMPLETE)
│   ├── skills/
│   │   ├── earnings_analyst.md
│   │   ├── market_researcher.md
│   │   └── valuation_reviewer.md
│   └── memory/                ← Agent reflection logs (JSON)
│
├── risk/
│   ├── risk_gate.py           ← Trade filter + kill switch
│   └── position_sizer.py      ← Fixed-fractional sizing
│
├── execution/
│   ├── alpaca.py              ← Alpaca paper + live
│   └── tiger.py               ← Tiger Brokers (SGX/HK/US)
│
├── monitoring/
│   ├── logger.py              ← JSON decision log + trades CSV
│   ├── telegram_alerts.py     ← Telegram notifications
│   └── dashboard.py           ← Streamlit dashboard
│
├── backtest/
│   └── nautilus_runner.py     ← NautilusTrader backtesting
│
├── scheduler.py               ← APScheduler watchlist runner
├── logs/                      ← Session JSON files (gitignored)
└── tests/
    ├── test_data_fetcher.py
    └── test_risk_gate.py
```

---

## 🔑 Environment Variables (.env)

```
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6

# Financial Data
ALPHA_VANTAGE_API_KEY=...    # Free 25 req/day — RSI/MACD/BB working
FINNHUB_API_KEY=...           # Free 60 req/min — quotes + news working
FMP_API_KEY=...               # Free tier — fundamentals blocked (403)
                              # Upgrade to $19/mo before paper trading

# Brokerage
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   ← paper trading

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Bot Config
WATCHLIST=AAPL,MSFT,GOOGL,NVDA,DBS.SI
MAX_POSITION_SIZE_PCT=0.05     ← 5% max per position
MAX_DAILY_LOSS_PCT=0.02        ← 2% daily loss kill switch
MIN_CONFIDENCE_SCORE=7         ← minimum to place trade
MAX_TRADES_PER_DAY=5
TRADING_MODE=paper             ← change to 'live' only after validation
```

---

## 🧠 Key Design Decisions

### LLM & Prompt Caching
- Model: `claude-sonnet-4-6` for all agents
- All system prompts use `cache_control: {"type": "ephemeral", "ttl": "1h"}`
- Bot runs every 30 min — default 5-min TTL would always expire
- Cache saves ~90% of Claude API costs on repeat runs
- Per-run market data goes in user message (NOT cached)
- Check `response.usage.cache_read_input_tokens` to verify caching works

### Trading Style
- **Daily swing trader** — NOT high frequency
- Runs at market open (9am SGT) and pre-close (3pm SGT)
- 1-3 high conviction trades per day maximum
- Holds positions for 1-5 days typically
- LLMs are too slow for HFT — this is intentional

### Confidence Threshold
- Minimum confidence 7.0/10 to place BUY or SELL
- Currently scoring 5.5 due to missing FMP fundamentals (free tier 403s)
- Will produce real signals once FMP upgraded to $19/mo
- Confidence scales position size: 7.x=50%, 8.x=75%, 9+x=100% of max

### SGX Stocks
- DBS.SI and other SGX tickers fail on US APIs
- Fix: add `yfinance` as fallback connector for Singapore stocks
- Not yet implemented — planned for Phase 5

### Decision Object Schema
```python
{
  "ticker":      str,
  "action":      "BUY" | "HOLD" | "SELL",
  "confidence":  float,   # 1.0-10.0
  "reasoning":   str,
  "bull_case":   str,
  "bear_case":   str,
  "risk_flags":  list[str],
  "analysed_at": ISO timestamp
}
```

### Risk Gate Schema
```python
{
  "approved":          bool,
  "action":            "BUY" | "HOLD" | "SELL" | "BLOCKED",
  "reason":            str,
  "original_decision": dict
}
```

### Position Sizing Schema
```python
{
  "ticker":           str,
  "action":           str,
  "entry_price":      float,
  "quantity":         int,       # whole shares only
  "position_value":   float,
  "position_pct":     float,
  "stop_loss_price":  float,     # 3% below entry for BUY
  "take_profit_price":float,     # 6% above entry for BUY (2:1 R:R)
  "risk_amount":      float,
  "max_position_value":float
}
```

---

## 🚦 Full Decision Flow (Single Trade Cycle)

```
Scheduler triggers (9am or 3pm SGT)
         │
         ▼
DataFetcher.fetch(ticker)
  ├── Alpha Vantage: RSI, MACD, Bollinger Bands, OHLCV
  ├── Finnhub: real-time quote, news (10 articles), earnings calendar
  └── FMP: fundamentals, ratios, earnings surprises, analyst targets
         │
         ▼
TradingAgentsWrapper.analyse(data)
  ├── Technical Analyst   → trend, momentum, volatility
  ├── Fundamental Analyst → valuation, margins, earnings quality
  ├── Sentiment Analyst   → news tone, crowd sentiment
  ├── Bull Researcher     → strongest bull case
  ├── Bear Researcher     → strongest bear case
  ├── Risk Manager        → risk/reward, proceed/downgrade/veto
  └── Fund Manager        → BUY/HOLD/SELL + confidence 1-10
         │
         ▼
RiskGate.check(decision, portfolio_state)
  ├── confidence ≥ 7.0?
  ├── kill_switch.lock exists?
  ├── trades_today < MAX_TRADES_PER_DAY?
  ├── daily_pnl > -MAX_DAILY_LOSS_PCT?
  ├── critical risk flags? (halt/delist/fraud/bankruptcy)
  └── price data valid?
         │
    ┌────┴────┐
  BLOCKED   APPROVED
    │           │
    ▼           ▼
  Log it   PositionSizer.calculate()
  Alert      │
  Telegram   ▼
           Place order via Alpaca/Tiger
             │
             ▼
           Log trade → telegram_alerts.py
             │
             ▼
           Dashboard updates
```

---

## 📊 Phase 6 Dashboard Specification (monitoring/dashboard.py)

Build using **Streamlit**. The dashboard should be accessible at `localhost:8501`.

### Page 1 — Live Overview
- **Account Summary**: total equity, cash available, daily P&L ($  and %), 
  unrealised P&L, realised P&L
- **Bot Status**: last run time, next run time, kill switch status (ON/OFF button)
- **Watchlist Table**: for each ticker — current price, change%, bot signal 
  (BUY/HOLD/SELL), confidence score, last analysed

### Page 2 — Open Positions
- Table: ticker, entry price, current price, quantity, market value, 
  unrealised P&L ($  and %), stop loss price, take profit price, days held
- Colour code: green for profitable, red for loss
- Close position button (sends market sell order)

### Page 3 — Trade History
- Full trade log table: date, ticker, action, quantity, entry price, 
  exit price, P&L ($  and %), hold duration
- Cumulative P&L chart (line chart over time)
- Win rate, average win, average loss, profit factor, Sharpe ratio
- Best trade, worst trade

### Page 4 — Bot Decisions (Agent Reasoning Viewer)
- Session selector (dropdown of past sessions by date)
- For selected session, show each ticker:
  - Final decision + confidence
  - Risk Gate result (approved/blocked + reason)
  - Position sizing details
  - Full agent reasoning (expandable sections):
    - Technical Analysis
    - Fundamental Analysis
    - Sentiment Analysis
    - Bull Case
    - Bear Case
    - Risk Assessment
    - Fund Manager reasoning

### Page 5 — Performance Analytics
- Equity curve vs benchmark (SPY)
- Monthly returns heatmap
- Drawdown chart
- Performance by ticker (which stocks made/lost money)
- Performance by action type (BUY vs SELL accuracy)
- Agent confidence vs actual outcome scatter plot

### Page 6 — Settings & Controls
- Kill switch toggle (creates/removes kill_switch.lock)
- Watchlist editor (add/remove tickers)
- Risk parameter editor (confidence threshold, max position size etc)
- Manual trigger: run analysis now button
- API status panel: shows which APIs are responding

---

## 💰 Monthly Running Costs

| Phase | Item | Cost |
|-------|------|------|
| Now | Claude API (with caching) | ~$5-15/mo |
| Now | Alpha Vantage free | $0 |
| Now | Finnhub free | $0 |
| Now | FMP free | $0 |
| **Phase 5** | **Upgrade FMP to Starter** | **$19/mo** |
| **Phase 5** | **Upgrade Finnhub if needed** | **$49+/mo** |
| Phase 6 | VPS (Hetzner CX21) | ~$6/mo |
| Phase 6 | TradingView Essential | ~$13/mo |

**Current total: ~$5-15/mo**
**Paper trading total: ~$24-34/mo**
**Live trading total: ~$43-93/mo**

---

## ⚠️ Known Issues & Planned Fixes

| Issue | Impact | Fix |
|-------|--------|-----|
| FMP free tier blocks fundamentals (403) | Confidence scores capped at ~5.5 → all HOLDs | Upgrade FMP to $19/mo at Phase 5 |
| DBS.SI fails on US APIs | SGX stocks unusable | Add yfinance fallback connector |
| Finnhub sentiment blocked on free tier | No sentiment scores | Use news headlines as proxy (working) |
| Alpha Vantage 25 req/day limit | Limited to ~3 tickers/day on free | Upgrade or cache aggressively |

---

## 🔧 Coding Conventions

- **Language**: Python 3.11+
- **Imports**: standard library first, then third-party, then local
- **Environment**: always use `load_dotenv()` at module level
- **Logging**: use `logging.getLogger(__name__)` — never print() in production code
- **Error handling**: all external API calls wrapped in try/except — never crash on data failure
- **Type hints**: use them on all public methods
- **Docstrings**: module-level docstring explaining purpose, then method docstrings
- **Constants**: uppercase, defined at module level (e.g. MODEL = "claude-sonnet-4-6")
- **Prompt caching**: ALL system prompts must use `_cache_system()` helper with `ttl="1h"`
- **Data**: never mutate the raw DataFetcher output — always work on copies
- **Tests**: pytest, mock all external API calls, tests live in tests/

---

## 🚀 How to Run

```bash
# Activate environment
cd ~/Desktop/ai-trading-bot
source venv/bin/activate

# Single ticker analysis
python run_analysis.py --ticker AAPL

# Full watchlist
python run_analysis.py --watchlist

# Run dashboard (Phase 6)
streamlit run monitoring/dashboard.py

# Run tests
pytest tests/ -v

# Activate kill switch
python -c "from risk.risk_gate import RiskGate; RiskGate().kill_switch()"

# Deactivate kill switch
rm kill_switch.lock
```

---

## 📡 External Services

| Service | Purpose | Status | Docs |
|---------|---------|--------|------|
| TradingView Desktop | Live charts via MCP | ✅ Connected | Launch with `--remote-debugging-port=9222` |
| tradingview-mcp server | Bridge to TradingView | ✅ Installed | `node ~/Desktop/tradingview-mcp/src/server.js` |
| Claude Code MCP | tradingview-mcp registered | ✅ Connected | `claude mcp list` |
| Anthropic API | LLM for all agents | ✅ Working | console.anthropic.com |
| Alpha Vantage | Technical indicators | ✅ Working (free) | alphavantage.co |
| Finnhub | Quotes + news | ✅ Partial (free) | finnhub.io |
| FMP | Fundamentals | ⚠️ Blocked (free) | financialmodelingprep.com |
| Alpaca | Paper trading | ⬜ Not yet connected | alpaca.markets |
| Tiger Brokers | SGX live trading | ⬜ Phase 6 | tigerbrokers.com.sg |
| Telegram | Alerts | ⬜ Phase 5 | core.telegram.org/bots |

---

*Owner: jerometan9742 | Repo: github.com/jerometan9742/TradingBot*
*Last updated: Phase 4 in progress*
