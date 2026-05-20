"""
TradingAgentsWrapper — runs a multi-agent analysis pipeline on a single ticker.

Pipeline:
    1. Technical Analyst   — reads price action & indicators
    2. Fundamental Analyst — reads financials & ratios
    3. Sentiment Analyst   — reads news & crowd sentiment
    4. Bull Researcher     — synthesises the bull case
    5. Bear Researcher     — synthesises the bear case
    6. Risk Manager        — veto-checks proposed action
    7. Fund Manager        — final BUY / HOLD / SELL decision

Prompt caching:
    All system prompts use cache_control ttl="1h" so they survive the
    30-minute bot cycle between runs (the default 5-min TTL would always
    expire).  Only the per-run market data sits in the user message.

Usage:
    wrapper = TradingAgentsWrapper()
    data    = DataFetcher().fetch("AAPL")
    decision = wrapper.analyse(data)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Stable system prompts — NEVER include timestamps or per-run IDs here.
# These are the cached prefix; dynamic content goes in user messages.
# ---------------------------------------------------------------------------

_SYS_TECHNICAL = """\
You are an expert Technical Analyst at a quantitative hedge fund.
Your job is to evaluate price action and technical indicators for a single stock.

You receive a JSON snapshot containing:
- quote:      current price, change%, high/low/open/prev_close
- technicals: RSI-14, MACD, MACD signal, MACD histogram, Bollinger Bands

Produce a concise technical analysis covering:
1. Trend direction (bullish / neutral / bearish) and strength
2. Momentum signals (RSI overbought/oversold, MACD crossovers)
3. Volatility context (position within Bollinger Bands)
4. Key support / resistance levels implied by the data
5. Short-term technical outlook (1-4 weeks)

Be precise and data-driven. Reference specific indicator values."""

_SYS_FUNDAMENTAL = """\
You are an expert Fundamental Analyst at a value-oriented investment fund.
Your job is to assess the intrinsic value and financial health of a stock.

You receive a JSON snapshot containing:
- fundamentals: PE, PB, EV/EBITDA, ROE, debt/equity, revenue, net income,
                gross margin, operating margin, company description
- earnings:     latest EPS vs estimate, surprise%, earnings history
- analyst:      buy/hold/sell counts, price targets

Produce a concise fundamental analysis covering:
1. Valuation: cheap, fair, or expensive vs historical norms
2. Profitability and margin trends
3. Balance-sheet risk (leverage)
4. Earnings quality and recent surprises
5. Analyst consensus and price-target implied upside/downside
6. Medium-term fundamental outlook (3-12 months)

Be precise and cite specific numbers from the data."""

_SYS_SENTIMENT = """\
You are an expert Sentiment Analyst at a macro research firm.
Your job is to gauge market mood and news flow around a stock.

You receive a JSON snapshot containing:
- sentiment:  bullish%, bearish%, article count, weekly average
- news:       up to 10 recent headlines with source and summary

Produce a concise sentiment analysis covering:
1. Overall market mood (bullish / neutral / bearish) and confidence
2. Key themes in recent news (positive or negative catalysts)
3. Sentiment momentum (improving, stable, deteriorating)
4. Any red flags or tail risks from the news
5. Short-term sentiment outlook

Be precise; quote headlines where relevant."""

_SYS_BULL = """\
You are the Bull Researcher in an investment committee debate.
Your role is to construct the strongest possible bull case for buying a stock.

You receive:
- technical_analysis:   findings from the Technical Analyst
- fundamental_analysis: findings from the Fundamental Analyst
- sentiment_analysis:   findings from the Sentiment Analyst

Build a persuasive, evidence-based bull case that:
1. Highlights 3-5 key tailwinds or catalysts
2. Explains why current valuation is attractive
3. Projects a realistic 12-month price target with rationale
4. Addresses likely bear objections

Respond with a structured argument, not a list of bullet points."""

_SYS_BEAR = """\
You are the Bear Researcher in an investment committee debate.
Your role is to construct the strongest possible bear case against a stock.

You receive:
- technical_analysis:   findings from the Technical Analyst
- fundamental_analysis: findings from the Fundamental Analyst
- sentiment_analysis:   findings from the Sentiment Analyst

Build a persuasive, evidence-based bear case that:
1. Highlights 3-5 key risks or headwinds
2. Explains why current valuation is stretched or the business is deteriorating
3. Projects a realistic 12-month downside scenario
4. Addresses likely bull objections

Respond with a structured argument, not a list of bullet points."""

_SYS_RISK = """\
You are the Risk Manager at a systematic hedge fund.
Your job is to identify risks in a proposed trade recommendation and decide
whether to proceed, downgrade, or veto it.

You receive:
- proposed_action: BUY / HOLD / SELL with a confidence score
- bull_case:       the bull researcher's argument
- bear_case:       the bear researcher's argument
- market_data:     raw snapshot (quote, technicals, fundamentals, sentiment)

Assess:
1. Downside risk vs. upside potential (risk/reward ratio)
2. Position sizing implications given volatility
3. Macro or event-driven risks not captured by the data
4. Whether the proposed action should be confirmed, downgraded, or vetoed

Return your risk assessment as structured text with a clear PROCEED / DOWNGRADE /
VETO recommendation and the key risk flags (as a list)."""

_SYS_FUND_MANAGER = """\
You are the Fund Manager making the final investment decision.
You chair the investment committee and have heard all analysts.

You receive:
- technical_analysis:   Technical Analyst report
- fundamental_analysis: Fundamental Analyst report
- sentiment_analysis:   Sentiment Analyst report
- bull_case:            Bull Researcher argument
- bear_case:            Bear Researcher argument
- risk_assessment:      Risk Manager assessment

Make the final decision. You MUST respond with a single valid JSON object
matching this schema exactly — no prose before or after the JSON:

{
  "action":      "BUY" | "HOLD" | "SELL",
  "confidence":  <float 1.0-10.0>,
  "reasoning":   "<2-4 sentence synthesis of why you made this call>",
  "bull_case":   "<1-2 sentence summary of the bull case>",
  "bear_case":   "<1-2 sentence summary of the bear case>",
  "risk_flags":  ["<risk 1>", "<risk 2>", ...]
}

Rules:
- confidence must be ≥ 7.0 to recommend BUY or SELL; otherwise default to HOLD
- risk_flags must be a list (can be empty)
- No markdown fences, no extra keys"""


def _cache_system(text: str) -> list[dict]:
    """Wrap a system prompt string in a cache_control block with 1-hour TTL."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def _format_data(data: dict) -> str:
    """Serialise the DataFetcher dict into a compact JSON string for the user message."""
    return json.dumps(data, default=str, indent=2)


def _repair_truncated_json(raw: str) -> str:
    """
    Best-effort recovery for a JSON object truncated at max_tokens.

    Strategy: walk the raw string, track open brackets/braces and whether
    we're inside a string literal, then close whatever is still open.
    This handles the most common truncation points: inside a string value,
    inside an array, or mid-object.
    """
    in_string = False
    escape_next = False
    depth_brace = 0
    depth_bracket = 0

    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1

    closing = ""
    if in_string:
        closing += '"'             # close the dangling string value
    closing += "]" * depth_bracket  # close open arrays
    closing += "}" * depth_brace    # close open objects

    repaired = raw + closing
    logger.debug("[FundManager] Truncation repair appended: %r", closing)
    return repaired


class TradingAgentsWrapper:
    """
    Runs a 7-stage multi-agent analysis pipeline using Claude Sonnet 4.6.

    Each agent call uses a stable, cacheable system prompt (1h TTL) so the
    30-minute bot cycle re-uses cached prefixes instead of paying full token
    cost on every run.
    """

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        self.client = anthropic.Anthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyse(self, data: dict) -> dict:
        """
        Run the full pipeline on pre-fetched ticker data.

        Args:
            data: Output of DataFetcher().fetch(ticker)

        Returns:
            Structured decision dict (see module docstring for schema).
        """
        ticker = data.get("ticker", "UNKNOWN")
        logger.info("[TradingAgents] Starting analysis for %s", ticker)

        try:
            technical   = self._run_technical(data)
            fundamental = self._run_fundamental(data)
            sentiment   = self._run_sentiment(data)
            bull        = self._run_bull(technical, fundamental, sentiment)
            bear        = self._run_bear(technical, fundamental, sentiment)
            risk        = self._run_risk(data, bull, bear)
            decision    = self._run_fund_manager(ticker, technical, fundamental, sentiment, bull, bear, risk)
        except Exception as exc:
            logger.error("[TradingAgents] Pipeline failed for %s: %s", ticker, exc, exc_info=True)
            decision = self._hold_decision(ticker, str(exc))

        logger.info(
            "[TradingAgents] %s → %s (confidence %.1f)",
            ticker, decision["action"], decision["confidence"],
        )
        return decision

    # ------------------------------------------------------------------
    # Agent stages
    # ------------------------------------------------------------------

    def _run_technical(self, data: dict) -> str:
        logger.info("[TechnicalAnalyst] Analysing technicals...")
        subset = {"ticker": data.get("ticker"), "quote": data.get("quote", {}), "technicals": data.get("technicals", {})}
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_TECHNICAL),
                messages=[{"role": "user", "content": f"Analyse the following data:\n\n{_format_data(subset)}"}],
            )
            result = response.content[0].text
            logger.debug("[TechnicalAnalyst] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[TechnicalAnalyst] Done.")
            return result
        except Exception as exc:
            logger.warning("[TechnicalAnalyst] Failed: %s", exc)
            return f"Technical analysis unavailable: {exc}"

    def _run_fundamental(self, data: dict) -> str:
        logger.info("[FundamentalAnalyst] Analysing fundamentals...")
        subset = {
            "ticker":       data.get("ticker"),
            "fundamentals": data.get("fundamentals", {}),
            "earnings":     data.get("earnings", {}),
            "analyst":      data.get("analyst", {}),
        }
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_FUNDAMENTAL),
                messages=[{"role": "user", "content": f"Analyse the following data:\n\n{_format_data(subset)}"}],
            )
            result = response.content[0].text
            logger.debug("[FundamentalAnalyst] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[FundamentalAnalyst] Done.")
            return result
        except Exception as exc:
            logger.warning("[FundamentalAnalyst] Failed: %s", exc)
            return f"Fundamental analysis unavailable: {exc}"

    def _run_sentiment(self, data: dict) -> str:
        logger.info("[SentimentAnalyst] Analysing sentiment...")
        subset = {"ticker": data.get("ticker"), "sentiment": data.get("sentiment", {}), "news": data.get("news", [])}
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_SENTIMENT),
                messages=[{"role": "user", "content": f"Analyse the following data:\n\n{_format_data(subset)}"}],
            )
            result = response.content[0].text
            logger.debug("[SentimentAnalyst] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[SentimentAnalyst] Done.")
            return result
        except Exception as exc:
            logger.warning("[SentimentAnalyst] Failed: %s", exc)
            return f"Sentiment analysis unavailable: {exc}"

    def _run_bull(self, technical: str, fundamental: str, sentiment: str) -> str:
        logger.info("[BullResearcher] Building bull case...")
        payload = json.dumps({
            "technical_analysis":   technical,
            "fundamental_analysis": fundamental,
            "sentiment_analysis":   sentiment,
        }, indent=2)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_BULL),
                messages=[{"role": "user", "content": f"Build the bull case from these analyses:\n\n{payload}"}],
            )
            result = response.content[0].text
            logger.debug("[BullResearcher] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[BullResearcher] Done.")
            return result
        except Exception as exc:
            logger.warning("[BullResearcher] Failed: %s", exc)
            return f"Bull case unavailable: {exc}"

    def _run_bear(self, technical: str, fundamental: str, sentiment: str) -> str:
        logger.info("[BearResearcher] Building bear case...")
        payload = json.dumps({
            "technical_analysis":   technical,
            "fundamental_analysis": fundamental,
            "sentiment_analysis":   sentiment,
        }, indent=2)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_BEAR),
                messages=[{"role": "user", "content": f"Build the bear case from these analyses:\n\n{payload}"}],
            )
            result = response.content[0].text
            logger.debug("[BearResearcher] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[BearResearcher] Done.")
            return result
        except Exception as exc:
            logger.warning("[BearResearcher] Failed: %s", exc)
            return f"Bear case unavailable: {exc}"

    def _run_risk(self, data: dict, bull: str, bear: str) -> str:
        logger.info("[RiskManager] Performing risk check...")
        payload = json.dumps({
            "proposed_action": "Pending Fund Manager decision",
            "bull_case":       bull,
            "bear_case":       bear,
            "market_data": {
                "quote":        data.get("quote", {}),
                "technicals":   data.get("technicals", {}),
                "fundamentals": data.get("fundamentals", {}),
                "sentiment":    data.get("sentiment", {}),
            },
        }, indent=2)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_RISK),
                messages=[{"role": "user", "content": f"Assess the risk of this trade:\n\n{payload}"}],
            )
            result = response.content[0].text
            logger.debug("[RiskManager] cache_read=%d", response.usage.cache_read_input_tokens)
            logger.info("[RiskManager] Done.")
            return result
        except Exception as exc:
            logger.warning("[RiskManager] Failed: %s", exc)
            return f"Risk assessment unavailable: {exc}"

    def _run_fund_manager(
        self,
        ticker: str,
        technical: str,
        fundamental: str,
        sentiment: str,
        bull: str,
        bear: str,
        risk: str,
    ) -> dict:
        logger.info("[FundManager] Making final decision...")
        payload = json.dumps({
            "ticker":               ticker,
            "technical_analysis":   technical,
            "fundamental_analysis": fundamental,
            "sentiment_analysis":   sentiment,
            "bull_case":            bull,
            "bear_case":            bear,
            "risk_assessment":      risk,
        }, indent=2)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1536,
                system=_cache_system(_SYS_FUND_MANAGER),
                messages=[{"role": "user", "content": f"Make the final investment decision:\n\n{payload}"}],
            )
            raw = response.content[0].text.strip()
            logger.debug(
                "[FundManager] cache_read=%d stop_reason=%s",
                response.usage.cache_read_input_tokens,
                response.stop_reason,
            )

            if response.stop_reason == "max_tokens":
                logger.warning("[FundManager] Response truncated at max_tokens — attempting JSON recovery")
                raw = _repair_truncated_json(raw)

            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            parsed = json.loads(raw)

            decision = {
                "ticker":       ticker,
                "action":       parsed.get("action", "HOLD").upper(),
                "confidence":   float(parsed.get("confidence", 5.0)),
                "reasoning":    parsed.get("reasoning", ""),
                "bull_case":    parsed.get("bull_case", ""),
                "bear_case":    parsed.get("bear_case", ""),
                "risk_flags":   parsed.get("risk_flags", []),
                "analysed_at":  datetime.now(timezone.utc).isoformat(),
            }

            # Safety guard: enforce confidence threshold
            if decision["action"] in ("BUY", "SELL") and decision["confidence"] < 7.0:
                logger.warning(
                    "[FundManager] Confidence %.1f < 7.0; downgrading %s → HOLD",
                    decision["confidence"], decision["action"],
                )
                decision["action"] = "HOLD"
                decision["risk_flags"].append("Confidence below threshold — downgraded to HOLD")

            logger.info("[FundManager] Decision: %s (confidence %.1f)", decision["action"], decision["confidence"])
            return decision

        except json.JSONDecodeError as exc:
            logger.error("[FundManager] Failed to parse JSON response: %s\nRaw: %s", exc, raw if "raw" in dir() else "N/A")
            return self._hold_decision(ticker, f"JSON parse error: {exc}")
        except Exception as exc:
            logger.error("[FundManager] Failed: %s", exc, exc_info=True)
            return self._hold_decision(ticker, str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hold_decision(ticker: str, reason: str) -> dict:
        """Return a safe HOLD decision when any agent fails."""
        return {
            "ticker":      ticker,
            "action":      "HOLD",
            "confidence":  1.0,
            "reasoning":   f"Pipeline error — defaulting to HOLD. Reason: {reason}",
            "bull_case":   "",
            "bear_case":   "",
            "risk_flags":  [f"Pipeline failure: {reason}"],
            "analysed_at": datetime.now(timezone.utc).isoformat(),
        }
