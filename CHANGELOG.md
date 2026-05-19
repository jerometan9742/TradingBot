# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org)

---

## [Unreleased]

### Added
- Initial project scaffold and repository structure
- Phase 1 data pipeline:
  - `AlphaVantageClient` — OHLCV, fundamentals, 50+ technical indicators, news sentiment
  - `FinnhubClient` — real-time quotes, earnings calendar, news, sentiment, analyst data
  - `FMPClient` — income statements, balance sheets, cash flows, key ratios, DCF
  - `DataFetcher` — unified normaliser that combines all sources into one structured dict
- `Logger` — structured JSON decision logging + trades CSV
- Unit tests for `DataFetcher` with mock API clients
- `.env.example` with all required environment variables documented
- `requirements.txt` with pinned dependencies
- `.gitignore` covering Python, secrets, logs, IDE files
- `CHANGELOG.md` (this file)

---

## Planned

### Phase 2 — TradingView MCP
- `ChartReader` — wrapper for TradingView MCP tools (OHLCV, indicators, screenshots)

### Phase 3 — TradingAgents
- Multi-agent pipeline: Technical, Fundamental, Sentiment analysts
- Bull vs Bear researcher debate layer
- Risk Manager veto + Fund Manager final decision
- Prompt caching for all agent system prompts (1-hour TTL)
- Agent memory/reflection loop

### Phase 4 — Risk & Portfolio Logic
- `RiskGate` — confidence threshold, position limits, kill switch
- `PositionSizer` — Kelly criterion / fixed-fraction sizing

### Phase 5 — Paper Trading
- Alpaca paper trading integration
- APScheduler watchlist runner
- Telegram alerts (trade placed, rejected, daily P&L)
- NautilusTrader backtesting runner

### Phase 6 — Live Trading
- Tiger Brokers / Interactive Brokers connector
- Streamlit P&L monitoring dashboard
- Docker deployment + VPS setup guide
