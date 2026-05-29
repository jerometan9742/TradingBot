"""
ReflectionEngine — post-trade analysis and lesson extraction for the AI trading bot.

After a position is closed, ReflectionEngine:
  1. Pairs the BUY entry row with its matching SELL/FILLED exit row from trades.csv.
  2. Retrieves the agent reasoning and technical snapshot from the nearest session log.
  3. Calls Claude to produce a structured JSON reflection (outcome, lessons, patterns).
  4. Persists the reflection to agents/memory/reflections/{trade_id}.json.
  5. Rebuilds lessons_learned.md from all stored reflections so agents can load it
     as context on the next run.
"""

import csv
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

_MEMORY_DIR      = Path(__file__).resolve().parent
_REFLECTIONS_DIR = _MEMORY_DIR / "reflections"
_LESSONS_FILE    = _MEMORY_DIR / "lessons_learned.md"
_LOG_DIR         = Path(__file__).resolve().parent.parent.parent / "logs"
_TRADES_CSV      = _LOG_DIR / "trades.csv"

_SYS_REFLECTION = """\
You are a trading reflection engine for an AI trading bot. Your job is to analyse \
completed trades and extract specific, actionable lessons that will improve future \
trading decisions.

Be brutally honest. If the bot made a mistake, say so clearly. If it made a good \
decision that went wrong due to market conditions, note that too. Focus on PATTERNS, \
not one-off events."""


def _cache_system(text: str) -> list[dict]:
    """Wrap a system prompt string in a cache_control block with 1-hour TTL."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class ReflectionEngine:
    """Generates and stores post-trade reflections using Claude."""

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        _REFLECTIONS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reflect_on_closed_trade(
        self,
        ticker: str,
        entry_row: dict,
        exit_row: dict,
    ) -> Optional[dict]:
        """Generate and store a reflection for one completed trade."""
        trade_id    = _trade_id(entry_row)
        try:
            entry_ts    = _parse_ts(entry_row["timestamp"])
            session_data = self._find_session_data(ticker, entry_ts)
            ctx          = _build_trade_context(ticker, entry_row, exit_row, session_data)
            reflection   = self._generate_reflection(ctx)
            if reflection is None:
                return None
            self._save_reflection(ticker, entry_row, reflection)
            self._update_lessons_learned()
            return reflection
        except Exception:
            logger.exception("reflect_on_closed_trade failed for %s trade_id=%s", ticker, trade_id)
            return None

    def reflect_on_last_trade(self, ticker: str) -> Optional[dict]:
        """Reflect on the most recently closed trade for a ticker."""
        pairs = self._load_closed_trades(ticker)
        if not pairs:
            logger.info("No closed trades found for %s", ticker)
            return None
        entry_row, exit_row = pairs[-1]
        return self.reflect_on_closed_trade(ticker, entry_row, exit_row)

    def run_pending_reflections(self) -> int:
        """Generate reflections for every closed trade that lacks one. Returns count."""
        all_trades = self._load_all_closed_trades()
        count = 0
        for ticker, entry_row, exit_row in all_trades:
            trade_id = _trade_id(entry_row)
            if self._reflection_exists(ticker, trade_id):
                continue
            result = self.reflect_on_closed_trade(ticker, entry_row, exit_row)
            if result is not None:
                count += 1
        return count

    def get_recent_lessons(self, limit: int = 10) -> list[dict]:
        """Return the most recent `limit` reflections sorted by trade_date descending."""
        reflections = self._load_all_reflections()
        reflections.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        return reflections[:limit]

    def get_lessons_for_ticker(self, ticker: str) -> list[dict]:
        """Return all reflections for a specific ticker, newest first."""
        results = []
        pattern = f"{ticker.upper()}_*.json"
        for path in _REFLECTIONS_DIR.glob(pattern):
            try:
                results.append(json.loads(path.read_text()))
            except Exception:
                logger.warning("Could not load reflection %s", path)
        results.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        return results

    def get_calibration_stats(self) -> dict:
        """
        Compute win-rate by confidence band.

        Returns:
            {"high": {"wins": int, "total": int, "rate": float},
             "medium": {...}, "low": {...}}
        """
        buckets: dict[str, dict] = {
            "high":   {"wins": 0, "total": 0, "rate": 0.0},
            "medium": {"wins": 0, "total": 0, "rate": 0.0},
            "low":    {"wins": 0, "total": 0, "rate": 0.0},
        }
        for ref in self._load_all_reflections():
            conf = ref.get("confidence_at_entry")
            if conf is None:
                continue
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                continue
            if conf >= 8.0:
                band = "high"
            elif conf >= 7.0:
                band = "medium"
            else:
                band = "low"
            buckets[band]["total"] += 1
            if ref.get("outcome") == "WIN":
                buckets[band]["wins"] += 1
        for band, data in buckets.items():
            if data["total"] > 0:
                data["rate"] = round(data["wins"] / data["total"] * 100, 1)
        return buckets

    def build_lessons_context(
        self,
        ticker: str,
        market_condition: Optional[str] = None,
        recent_limit: int = 5,
    ) -> str:
        """
        Assemble a lessons context string for injection into an agent user message.

        Combines ticker-specific lessons, market-condition-matched lessons,
        recent lessons, and calibration stats.
        """
        parts: list[str] = []

        ticker_lessons = self.get_lessons_for_ticker(ticker)[:3]
        if ticker_lessons:
            parts.append(f"=== Past lessons for {ticker.upper()} ===")
            for ref in ticker_lessons:
                parts.append(
                    f"[{ref.get('trade_date', 'unknown')}] "
                    f"{ref.get('outcome', '?')} | "
                    f"{ref.get('key_lesson', '')}"
                )

        if market_condition:
            mc_upper = market_condition.upper()
            mc_lessons = [
                r for r in self._load_all_reflections()
                if r.get("market_condition", "").upper() == mc_upper
            ]
            mc_lessons.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
            mc_lessons = mc_lessons[:3]
            if mc_lessons:
                parts.append(f"\n=== Lessons in {mc_upper} markets ===")
                for ref in mc_lessons:
                    parts.append(
                        f"[{ref.get('ticker', '?')} {ref.get('trade_date', 'unknown')}] "
                        f"{ref.get('outcome', '?')} | "
                        f"{ref.get('key_lesson', '')}"
                    )

        recent = self.get_recent_lessons(recent_limit)
        if recent:
            parts.append(f"\n=== {len(recent)} most recent trade lessons ===")
            for ref in recent:
                parts.append(
                    f"[{ref.get('ticker', '?')} {ref.get('trade_date', 'unknown')}] "
                    f"{ref.get('outcome', '?')} conf={ref.get('confidence_at_entry', '?')} | "
                    f"{ref.get('key_lesson', '')}"
                )

        stats = self.get_calibration_stats()
        cal_lines = []
        for band in ("high", "medium", "low"):
            d = stats[band]
            if d["total"] > 0:
                cal_lines.append(
                    f"  {band.capitalize()} confidence (≥{'8.0' if band=='high' else '7.0' if band=='medium' else '<7.0'}): "
                    f"{d['rate']}% win rate ({d['wins']}/{d['total']})"
                )
        if cal_lines:
            parts.append("\n=== Confidence calibration ===")
            parts.extend(cal_lines)

        return "\n".join(parts)

    def generate_weekly_summary(self) -> dict:
        """
        Summarise all reflections from the last 7 days.

        Returns a dict with trade counts, win rate, best/worst trades,
        new lessons, top patterns, condition-specific win rates, and a
        warning pattern derived from losses.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = [
            r for r in self._load_all_reflections()
            if _parse_date(r.get("trade_date", "")) >= cutoff
        ]

        wins   = [r for r in recent if r.get("outcome") == "WIN"]
        losses = [r for r in recent if r.get("outcome") == "LOSS"]

        best_trade: dict  = {}
        worst_trade: dict = {}
        if recent:
            best_trade  = max(recent, key=lambda r: float(r.get("pnl", 0) or 0))
            worst_trade = min(recent, key=lambda r: float(r.get("pnl", 0) or 0))

        new_lessons = [r.get("key_lesson", "") for r in recent if r.get("key_lesson")]

        tag_counts: dict[str, int] = defaultdict(int)
        for r in recent:
            for tag in r.get("pattern_tags", []):
                tag_counts[tag] += 1
        top_patterns = dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True))

        def _condition_rate(condition: str) -> float:
            cond_refs = [r for r in recent if r.get("market_condition", "").upper() == condition]
            if not cond_refs:
                return 0.0
            cond_wins = sum(1 for r in cond_refs if r.get("outcome") == "WIN")
            return round(cond_wins / len(cond_refs) * 100, 1)

        loss_tag_counts: dict[str, int] = defaultdict(int)
        for r in losses:
            for tag in r.get("pattern_tags", []):
                loss_tag_counts[tag] += 1
        warning_pattern = max(loss_tag_counts, key=lambda t: loss_tag_counts[t]) if loss_tag_counts else ""

        return {
            "total_trades":      len(recent),
            "wins":              len(wins),
            "losses":            len(losses),
            "win_rate":          round(len(wins) / len(recent) * 100, 1) if recent else 0.0,
            "best_trade":        best_trade,
            "worst_trade":       worst_trade,
            "new_lessons":       new_lessons,
            "top_patterns":      top_patterns,
            "trending_win_rate": _condition_rate("TRENDING"),
            "ranging_win_rate":  _condition_rate("RANGING"),
            "warning_pattern":   warning_pattern,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_closed_trades(self, ticker: str) -> list[tuple[dict, dict]]:
        """
        FIFO-match BUY entries with SELL/FILLED exits for a single ticker.

        Returns a list of (entry_row, exit_row) pairs.
        """
        if not _TRADES_CSV.exists():
            return []
        try:
            rows = _read_csv(_TRADES_CSV)
        except Exception:
            logger.exception("Could not read %s", _TRADES_CSV)
            return []

        ticker_rows = [
            r for r in rows
            if r.get("ticker", "").upper() == ticker.upper()
        ]

        entries: list[dict] = []
        pairs:   list[tuple[dict, dict]] = []
        for row in ticker_rows:
            action = row.get("action", "").upper()
            if action == "BUY":
                entries.append(row)
            elif action in ("SELL", "FILLED") and entries:
                pairs.append((entries.pop(0), row))
        return pairs

    def _load_all_closed_trades(self) -> list[tuple[str, dict, dict]]:
        """
        FIFO-match trades for every ticker in trades.csv.

        Returns a list of (ticker, entry_row, exit_row).
        """
        if not _TRADES_CSV.exists():
            return []
        try:
            rows = _read_csv(_TRADES_CSV)
        except Exception:
            logger.exception("Could not read %s", _TRADES_CSV)
            return []

        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            ticker = row.get("ticker", "").upper()
            if ticker:
                by_ticker[ticker].append(row)

        result: list[tuple[str, dict, dict]] = []
        for ticker, ticker_rows in by_ticker.items():
            entries: list[dict] = []
            for row in ticker_rows:
                action = row.get("action", "").upper()
                if action == "BUY":
                    entries.append(row)
                elif action in ("SELL", "FILLED") and entries:
                    result.append((ticker, entries.pop(0), row))
        return result

    def _find_session_data(self, ticker: str, trade_timestamp: datetime) -> dict:
        """
        Find the session result dict for `ticker` whose generated_at is closest
        to `trade_timestamp` and within a ±48-hour window.

        Returns the result dict or {} if nothing matches.
        """
        best_result: dict  = {}
        best_delta: float  = float("inf")
        window = timedelta(hours=48)

        try:
            for session_path in sorted(_LOG_DIR.glob("session_*.json")):
                try:
                    session = json.loads(session_path.read_text())
                except Exception:
                    logger.warning("Could not parse session file %s", session_path)
                    continue

                gen_at = _parse_ts(session.get("generated_at", ""))
                if gen_at is None:
                    continue
                delta = abs((gen_at - trade_timestamp).total_seconds())
                if delta > window.total_seconds():
                    continue

                for result in session.get("results", []):
                    if result.get("ticker", "").upper() != ticker.upper():
                        continue
                    if delta < best_delta:
                        best_delta  = delta
                        best_result = result
        except Exception:
            logger.exception("Error scanning session files for %s", ticker)

        return best_result

    def _load_all_reflections(self) -> list[dict]:
        """Load every JSON file from _REFLECTIONS_DIR."""
        reflections = []
        for path in _REFLECTIONS_DIR.glob("*.json"):
            try:
                reflections.append(json.loads(path.read_text()))
            except Exception:
                logger.warning("Could not load reflection %s", path)
        return reflections

    def _generate_reflection(self, ctx: dict) -> Optional[dict]:
        """Call Claude to produce a structured reflection JSON from trade context."""
        user_prompt = _build_user_prompt(ctx)
        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=_cache_system(_SYS_REFLECTION),
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            logger.exception("Claude API call failed in _generate_reflection")
            return None

        raw = response.content[0].text if response.content else ""

        # Strip markdown fences if Claude wraps the JSON
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())

        try:
            parsed: dict = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Could not parse Claude reflection JSON: %s", raw[:200])
            return None

        # Infer outcome from P&L if Claude omitted it or gave unexpected value
        if parsed.get("outcome") not in ("WIN", "LOSS"):
            parsed["outcome"] = "WIN" if float(ctx.get("pnl", 0) or 0) >= 0 else "LOSS"

        # Attach trade metadata
        parsed["trade_id"]            = ctx["trade_id"]
        parsed["ticker"]              = ctx["ticker"]
        parsed["trade_date"]          = ctx["entry_date"]
        parsed["pnl"]                 = ctx["pnl"]
        parsed["pnl_pct"]             = ctx["pnl_pct"]
        parsed["confidence_at_entry"] = ctx["confidence"]
        parsed["exit_reason"]         = ctx["exit_reason"]

        return parsed

    def _save_reflection(self, ticker: str, entry_row: dict, reflection: dict) -> None:
        """Write a reflection dict to _REFLECTIONS_DIR/{trade_id}.json."""
        trade_id = _trade_id(entry_row)
        dest = _REFLECTIONS_DIR / f"{trade_id}.json"
        try:
            dest.write_text(json.dumps(reflection, indent=2, default=str))
            logger.info("Saved reflection %s", dest.name)
        except Exception:
            logger.exception("Could not save reflection to %s", dest)

    def _reflection_exists(self, ticker: str, trade_id: str) -> bool:
        """Return True if a reflection JSON already exists for this trade_id."""
        return (_REFLECTIONS_DIR / f"{trade_id}.json").exists()

    def _update_lessons_learned(self) -> None:
        """Rebuild lessons_learned.md from all stored reflections."""
        reflections = self._load_all_reflections()
        if not reflections:
            return

        reflections.sort(key=lambda r: r.get("trade_date", ""), reverse=True)

        # Group by ticker
        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for r in reflections:
            ticker = r.get("ticker", "UNKNOWN").upper()
            by_ticker[ticker].append(r)

        # Group by pattern tag
        by_tag: dict[str, list[dict]] = defaultdict(list)
        for r in reflections:
            for tag in r.get("pattern_tags", []):
                by_tag[tag].append(r)

        lines: list[str] = ["# Bot Trading Lessons", ""]

        # ── By Ticker ────────────────────────────────────────────────────
        lines.append("## By Ticker")
        lines.append("")
        for ticker in sorted(by_ticker.keys()):
            refs   = by_ticker[ticker]
            wins   = sum(1 for r in refs if r.get("outcome") == "WIN")
            rate   = round(wins / len(refs) * 100, 1) if refs else 0.0
            lines.append(f"### {ticker}")
            lines.append(f"Win rate: {rate}% ({wins}/{len(refs)} trades)")
            lines.append("")
            for ref in refs[:5]:
                lines.append(
                    f"- [{ref.get('trade_date', '?')}] "
                    f"**{ref.get('outcome', '?')}** "
                    f"(conf {ref.get('confidence_at_entry', '?')}) — "
                    f"{ref.get('key_lesson', '')}"
                )
            lines.append("")

        # ── By Pattern ───────────────────────────────────────────────────
        lines.append("## By Pattern")
        lines.append("")
        for tag in sorted(by_tag.keys()):
            refs = sorted(by_tag[tag], key=lambda r: r.get("trade_date", ""), reverse=True)
            lines.append(f"### {tag}")
            for ref in refs[:3]:
                wins_label = ref.get("outcome", "?")
                lines.append(
                    f"- [{ref.get('ticker', '?')} {ref.get('trade_date', '?')}] "
                    f"**{wins_label}** — {ref.get('key_lesson', '')}"
                )
            lines.append("")

        # ── Confidence Calibration ────────────────────────────────────────
        stats = self.get_calibration_stats()
        lines.append("## Confidence Calibration")
        lines.append("")
        for band, label in (("high", "High (≥8.0)"), ("medium", "Medium (7.0–7.9)"), ("low", "Low (<7.0)")):
            d = stats[band]
            if d["total"] > 0:
                lines.append(
                    f"- **{label}**: {d['rate']}% win rate ({d['wins']}/{d['total']} trades)"
                )
        lines.append("")

        # ── Market Condition Performance ──────────────────────────────────
        lines.append("## Market Condition Performance")
        lines.append("")
        cond_data: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
        for r in reflections:
            cond = r.get("market_condition", "UNKNOWN").upper()
            wins_so_far, total_so_far = cond_data[cond]
            new_wins  = wins_so_far + (1 if r.get("outcome") == "WIN" else 0)
            cond_data[cond] = (new_wins, total_so_far + 1)
        for cond in sorted(cond_data.keys()):
            w, t = cond_data[cond]
            rate  = round(w / t * 100, 1) if t > 0 else 0.0
            lines.append(f"- **{cond}**: {rate}% win rate ({w}/{t} trades)")
        lines.append("")

        try:
            _LESSONS_FILE.write_text("\n".join(lines))
            logger.info("Rebuilt %s (%d reflections)", _LESSONS_FILE.name, len(reflections))
        except Exception:
            logger.exception("Could not write %s", _LESSONS_FILE)


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, no I/O side effects)
# ---------------------------------------------------------------------------

def _trade_id(entry_row: dict) -> str:
    """Format trade_id as {TICKER}_{YYYYMMDD}_{HHMMSS}."""
    ticker = entry_row.get("ticker", "UNKNOWN").upper()
    ts     = _parse_ts(entry_row.get("timestamp", ""))
    if ts:
        return f"{ticker}_{ts.strftime('%Y%m%d_%H%M%S')}"
    return f"{ticker}_000000_000000"


def _build_trade_context(
    ticker: str,
    entry_row: dict,
    exit_row: dict,
    session_data: dict,
) -> dict:
    """
    Assemble a flat context dict used by both _generate_reflection and
    _build_user_prompt.
    """
    try:
        entry_price = float(entry_row.get("price", 0) or 0)
    except (TypeError, ValueError):
        entry_price = 0.0
    try:
        exit_price = float(exit_row.get("price", 0) or 0)
    except (TypeError, ValueError):
        exit_price = 0.0
    try:
        quantity = float(entry_row.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        quantity = 0.0

    pnl     = round((exit_price - entry_price) * quantity, 2)
    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2) if entry_price else 0.0

    # Determine exit reason
    try:
        stop_loss   = float(entry_row.get("stop_loss", 0) or 0)
        take_profit = float(entry_row.get("take_profit", 0) or 0)
    except (TypeError, ValueError):
        stop_loss = take_profit = 0.0

    if stop_loss and exit_price <= stop_loss * 1.01:
        exit_reason = "stop_loss"
    elif take_profit and exit_price >= take_profit * 0.99:
        exit_reason = "take_profit"
    else:
        exit_reason = "sell_signal"

    # Pull from session decision and market_data
    decision    = session_data.get("decision", {}) or {}
    market_data = session_data.get("market_data", {}) or {}
    technicals  = market_data.get("technicals", {}) or {}

    confidence = decision.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0

    agent_reasoning = decision.get("reasoning", "Not available")

    entry_ts = _parse_ts(entry_row.get("timestamp", ""))
    exit_ts  = _parse_ts(exit_row.get("timestamp", ""))

    return {
        "trade_id":       _trade_id(entry_row),
        "ticker":         ticker.upper(),
        "action":         entry_row.get("action", "BUY").upper(),
        "quantity":       quantity,
        "entry_price":    entry_price,
        "exit_price":     exit_price,
        "pnl":            pnl,
        "pnl_pct":        pnl_pct,
        "exit_reason":    exit_reason,
        "confidence":     confidence,
        "agent_reasoning": agent_reasoning,
        "rsi":            technicals.get("rsi_14", "N/A"),
        "macd":           technicals.get("macd", "N/A"),
        "macd_signal":    technicals.get("macd_signal", "N/A"),
        "atr_14":         technicals.get("atr_14", "N/A"),
        "entry_date":     entry_ts.strftime("%Y-%m-%d") if entry_ts else "unknown",
        "exit_date":      exit_ts.strftime("%Y-%m-%d") if exit_ts else "unknown",
    }


def _build_user_prompt(ctx: dict) -> str:
    """Render the user prompt string for the reflection API call."""
    return (
        f"Analyse this completed trade and write a structured reflection:\n\n"
        f"TRADE DATA:\n"
        f"{ctx['ticker']} | {ctx['action']} | {ctx['quantity']:.0f} shares\n"
        f"Entry: ${ctx['entry_price']:.2f} on {ctx['entry_date']}\n"
        f"Exit:  ${ctx['exit_price']:.2f} on {ctx['exit_date']}\n"
        f"P&L:   ${ctx['pnl']:.2f} ({ctx['pnl_pct']:.2f}%)\n"
        f"Exit reason: {ctx['exit_reason']}\n"
        f"Confidence at entry: {ctx['confidence']:.1f}/10\n\n"
        f"Technical conditions at entry:\n"
        f"RSI: {ctx['rsi']} | MACD: {ctx['macd']} | ADX: N/A\n"
        f"EMA20 vs EMA50: data unavailable\n\n"
        f"Agent reasoning at entry:\n"
        f"{ctx['agent_reasoning']}\n\n"
        f"Write a reflection covering:\n"
        f"1. Was the entry signal strong or weak? Why?\n"
        f"2. Was the exit (SL/TP/signal) appropriate?\n"
        f"3. What market conditions helped or hurt this trade?\n"
        f"4. What should the bot do differently next time?\n"
        f"5. Key lesson in one sentence\n\n"
        f"Respond with ONLY a valid JSON object — no prose outside the JSON:\n"
        f"{{\n"
        f'  "trade_id": "...",\n'
        f'  "ticker": "...",\n'
        f'  "outcome": "WIN or LOSS",\n'
        f'  "entry_quality": 1-10,\n'
        f'  "exit_quality": 1-10,\n'
        f'  "market_condition": "TRENDING or RANGING or VOLATILE",\n'
        f'  "key_lesson": "one sentence",\n'
        f'  "what_went_right": "string",\n'
        f'  "what_went_wrong": "string",\n'
        f'  "do_differently": "string",\n'
        f'  "confidence_calibration": "OVERCONFIDENT or UNDERCONFIDENT or ACCURATE",\n'
        f'  "pattern_tags": ["tag1", "tag2"]\n'
        f"}}"
    )


def _parse_ts(value: str) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp string (with or without timezone) into a
    timezone-aware datetime.  Returns None on failure.
    """
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    logger.debug("Could not parse timestamp: %s", value)
    return None


def _parse_date(value: str) -> datetime:
    """
    Parse a date string (YYYY-MM-DD or ISO timestamp) into a timezone-aware
    datetime.  Returns epoch on failure so sorting always works.
    """
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    result = _parse_ts(value)
    if result:
        return result
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _read_csv(path: Path) -> list[dict]:
    """Read a CSV file into a list of row dicts."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
