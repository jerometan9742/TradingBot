# AI Trading Bot

An autonomous stock analysis and trading system powered by Claude (Anthropic),
TradingAgents (TauricResearch), and TradingView MCP.

> ⚠️ **Disclaimer:** This project is for educational and research purposes.
> Always paper trade before using real capital.
> Nothing here constitutes financial or investment advice.

---

## Architecture

```
Data Layer          Analysis Layer (TradingAgents)     Execution Layer
──────────          ──────────────────────────────     ───────────────
TradingView MCP  →  Technical Analyst    ┐
Finnhub API      →  Fundamental Analyst  ├→ Bull/Bear → Risk Mgr → Fund Mgr → Alpaca / Tiger
Alpha Vantage    →  Sentiment Analyst    ┘
FMP API
```

## Build Phases

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Data pipeline (AV + Finnhub + FMP) | 🟡 In progress |
| 2 | TradingView MCP setup | ⬜ Planned |
| 3 | TradingAgents multi-agent analysis | ⬜ Planned |
| 4 | Risk gate & position sizing | ⬜ Planned |
| 5 | Paper trading (Alpaca) | ⬜ Planned |
| 6 | Live trading | ⬜ Planned |

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/YOUR_USERNAME/ai-trading-bot.git
cd ai-trading-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
# Edit .env and fill in your API keys

# 3. Test the data pipeline
python -c "from data.fetcher import DataFetcher; print(DataFetcher().fetch('AAPL'))"

# 4. Run tests
pytest tests/ -v
```

## Project Structure

```
ai-trading-bot/
├── data/               # Financial data connectors + unified fetcher
├── charts/             # TradingView MCP wrapper
├── agents/             # TradingAgents integration + custom skill prompts
├── risk/               # RiskGate + PositionSizer
├── execution/          # Brokerage API connectors (Alpaca, Tiger)
├── monitoring/         # Telegram alerts, Streamlit dashboard, logger
├── backtest/           # NautilusTrader backtesting runner
├── tests/              # Unit + integration tests
└── scheduler.py        # Watchlist runner / cron logic
```

## Monthly Running Costs

| Phase | Estimated Cost |
|-------|---------------|
| Phase 1–2 (building) | $13–18/mo |
| Phase 5 (paper trading) | $46–130/mo |
| Phase 6 (live trading) | $110–200/mo |

Key cost optimisation: prompt caching (1-hour TTL) reduces Claude API costs by ~90%.

## Tech Stack

- **Claude Sonnet 4.6** — LLM backbone for all agents
- **TradingAgents v0.2.4** — multi-agent framework
- **TradingView MCP** — live chart data via Chrome DevTools Protocol
- **Alpha Vantage** — OHLCV + 50+ technical indicators
- **Finnhub** — real-time quotes, news, sentiment
- **FMP** — fundamentals, ratios, DCF
- **Alpaca** — paper + live US stock trading (commission-free)
- **Tiger Brokers** — SGX, US, HK markets

---

*Built with [Claude](https://anthropic.com) · [TradingAgents](https://github.com/TauricResearch/TradingAgents) · [TradingView MCP](https://github.com/tradesdontlie/tradingview-mcp)*
