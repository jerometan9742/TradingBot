# AI Trading Bot — Project Memory

## What This Project Is
Automated stock trading bot using Claude AI (TradingAgents multi-agent pipeline), MooMoo/Futu
brokerage, running 24/7 on a VPS.

## Architecture
- VPS: 46.62.165.36 (Ubuntu 26.04)
- Broker: MooMoo/Futu via FutuOpenD
- Trading mode: PAPER (simulate account)
- Dashboard: Streamlit on port 8502
- Alerts: Telegram bot
- Auto-deploy: GitHub → VPS auto-pull every 5 mins

## Key Files
- scheduler.py — main bot runner + schedule
- agents/trading_agents.py — 7-agent pipeline
- execution/moomoo.py — MooMoo connector
- monitoring/dashboard.py — Streamlit dashboard
- monitoring/telegram_alerts.py — all Telegram commands
- monitoring/price_monitor.py — 24/7 SL/TP watcher
- agents/memory/reflection_engine.py — trade learning
- agents/memory/lessons_learned.md — bot memory
- data/screener.py — daily universe screener
- data/universe_builder.py — auto-builds universe
- risk/position_sizer.py — position sizing
- risk/risk_gate.py — trade filters
- watchlist.txt — active trading tickers
- watchlist_universe.txt — screener universe

## Trading Strategy (V2 Baseline — backtested)
- Timeframe: Daily
- ADX threshold: 15
- RSI entry: > 40
- ATR SL: 1.5x
- ATR TP: 3.0x (2:1 R:R)
- No EMA200 filter
- No Bollinger Band filter
- Backtested P&L: +7.39% META, +18.35% NVDA
- Win rate: ~41-44%

## Schedule (SGT)
- 6:00pm — Universe auto-update
- 7:00pm — Screener runs, Telegram top 15
- 8:00pm — Pre-market analysis (trade if conf >= 8.5)
- 9:30pm — NYSE open (trade if conf >= 7.0)
- 3:00am — Afternoon momentum (trade if conf >= 7.0)
- 4:00am — Market close P&L summary

## Critical Rules
- NEVER switch broker to Alpaca
- NEVER hardcode equity values
- NEVER push API keys or credentials
- ALWAYS use USD only (by_market US) for sizing
- ALWAYS test on VPS after pushing
- ALWAYS check CLAUDE.md before making changes
- Push to GitHub triggers auto-deploy to VPS

## Past Decisions & Why
- Switched from Alpaca to MooMoo: user has Futu app
- Removed Buying Power from dashboard: too wide
- Dashboard shows USD only not HKD: user preference
- BB filter removed: was blocking all trades
- EMA200 removed: too restrictive on daily timeframe
- ADX lowered 20→15: improved trade frequency
- RSI lowered 50→40: better entry timing
