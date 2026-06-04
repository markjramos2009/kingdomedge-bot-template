"""
KingdomEdge Algo — Risk Controls Module
========================================

Pre-trade risk checks that run BEFORE any order is placed.
Every check returns (allowed: bool, reason: str).

Controls:
  1. Daily loss limit         — halt trading after max daily drawdown
  2. Max position size        — cap per-subscriber quantity
  3. Duplicate signal guard   — reject same symbol+direction within cooldown
  4. Max open positions       — limit concurrent open trades
  5. Trading hours gate       — only trade during market hours
  6. Global kill switch       — admin can halt all trading instantly
  7. Notional dollar cap      — cap $ exposure per trade (price-aware)
  8. Consecutive losses pause — auto-halt after N losing trades in a row
  9. Account size minimum     — block subscribers below capital threshold

Configuration via environment variables (all optional — sensible defaults):
  RISK_MAX_DAILY_LOSS            — max daily loss in USD before halt (default: 500)
  RISK_MAX_POSITION_SIZE         — max contracts/shares per order (default: 10)
  RISK_COOLDOWN_SECONDS          — duplicate signal cooldown (default: 60)
  RISK_MAX_OPEN_POSITIONS        — max concurrent open positions (default: 3)
  RISK_TRADING_START_HOUR        — market open hour UTC (default: 14 = 10 AM ET)
  RISK_TRADING_END_HOUR          — market close hour UTC (default: 19 = 3 PM ET)
  RISK_MAX_NOTIONAL              — max $ exposure per trade (default: 1000)
  RISK_MAX_CONSECUTIVE_LOSSES    — auto-pause after N losses in a row (default: 3)
  RISK_MIN_ACCOUNT_VALUE         — min broker account equity (default: 0 = disabled; broker enforces its own minimums)
  RISK_ACCOUNT_VALUE_TTL_SECONDS — cache TTL for account equity fetch (default: 3600)
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────

MAX_DAILY_LOSS            = float(os.environ.get("RISK_MAX_DAILY_LOSS", "500"))
MAX_POSITION_SIZE         = int(os.environ.get("RISK_MAX_POSITION_SIZE", "10"))
COOLDOWN_SECONDS          = int(os.environ.get("RISK_COOLDOWN_SECONDS", "60"))
MAX_OPEN_POSITIONS        = int(os.environ.get("RISK_MAX_OPEN_POSITIONS", "3"))   # pilot: was 5
TRADING_START_HOUR        = int(os.environ.get("RISK_TRADING_START_HOUR", "14"))  # pilot: 10 AM ET in UTC (was 13)
TRADING_END_HOUR          = int(os.environ.get("RISK_TRADING_END_HOUR", "19"))    # pilot: 3 PM ET in UTC (was 20)

# Pilot additions (Gap 4 audit, 2026-05-22)
MAX_NOTIONAL              = float(os.environ.get("RISK_MAX_NOTIONAL", "1000"))
MAX_CONSECUTIVE_LOSSES    = int(os.environ.get("RISK_MAX_CONSECUTIVE_LOSSES", "3"))
MIN_ACCOUNT_VALUE         = float(os.environ.get("RISK_MIN_ACCOUNT_VALUE", "0"))
ACCOUNT_VALUE_TTL_SECONDS = int(os.environ.get("RISK_ACCOUNT_VALUE_TTL_SECONDS", "3600"))  # 1 hour cache

# ── State (thread-safe) ─────────────────────────────────────────

_lock = threading.Lock()

# Global kill switch — when True, ALL trading is halted
_kill_switch: bool = False

# Per-subscriber daily P&L tracking: { sub_id: { "date": "2026-04-20", "pnl": -123.45 } }
_daily_pnl: dict[str, dict] = {}

# Recent signals for duplicate detection: { "symbol:action": timestamp }
_recent_signals: dict[str, float] = {}

# Open position count per subscriber: { sub_id: count }
_open_positions: dict[str, int] = {}

# Consecutive loss streak per subscriber: { sub_id: count }
# Increments on losing trade, resets on winning trade.
_loss_streak: dict[str, int] = {}

# Cached broker account equity: { sub_id: {"value": float, "fetched_at": epoch_seconds} }
# Avoids hammering the broker on every signal.
_account_value_cache: dict[str, dict] = {}


# ── Helper: today's date string ──────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Kill Switch ──────────────────────────────────────────────────

def engage_kill_switch(reason: str = "manual"):
    """Halt all trading immediately."""
    global _kill_switch
    with _lock:
        _kill_switch = True
    log.critical(f"KILL SWITCH ENGAGED: {reason}")


def disengage_kill_switch():
    """Resume trading."""
    global _kill_switch
    with _lock:
        _kill_switch = False
    log.warning("Kill switch disengaged — trading resumed")


def is_kill_switch_on() -> bool:
    return _kill_switch


# ── Daily P&L Tracking ──────────────────────────────────────────

def record_trade_pnl(sub_id: str, pnl: float):
    """
    Record a realized P&L for a subscriber.
    Called when a trade closes (from fill callbacks or periodic checks).

    Also maintains the consecutive-loss streak: increments on a losing trade,
    resets to 0 on a winning trade. A trade with exactly $0 P&L is treated as
    neutral (no change to streak).
    """
    with _lock:
        today = _today()
        if sub_id not in _daily_pnl or _daily_pnl[sub_id]["date"] != today:
            _daily_pnl[sub_id] = {"date": today, "pnl": 0.0}
        _daily_pnl[sub_id]["pnl"] += pnl
        current = _daily_pnl[sub_id]["pnl"]
        log.info(f"[{sub_id}] Daily P&L updated: {current:+.2f} (trade: {pnl:+.2f})")

        if current <= -MAX_DAILY_LOSS:
            log.warning(f"[{sub_id}] Daily loss limit hit: {current:.2f} <= -{MAX_DAILY_LOSS}")

        # Update consecutive-loss streak
        if pnl < 0:
            _loss_streak[sub_id] = _loss_streak.get(sub_id, 0) + 1
            streak = _loss_streak[sub_id]
            log.info(f"[{sub_id}] Loss streak: {streak} (trade: {pnl:+.2f})")
            if streak >= MAX_CONSECUTIVE_LOSSES:
                log.warning(
                    f"[{sub_id}] Consecutive-loss limit hit: "
                    f"{streak} losses in a row (limit: {MAX_CONSECUTIVE_LOSSES})"
                )
        elif pnl > 0:
            prev = _loss_streak.get(sub_id, 0)
            if prev > 0:
                log.info(f"[{sub_id}] Loss streak reset (was {prev}, winning trade)")
            _loss_streak[sub_id] = 0
        # pnl == 0 → no change to streak


def get_daily_pnl(sub_id: str) -> float:
    """Get today's cumulative P&L for a subscriber."""
    with _lock:
        entry = _daily_pnl.get(sub_id, {})
        if entry.get("date") != _today():
            return 0.0
        return entry.get("pnl", 0.0)


def reset_daily_pnl(sub_id: Optional[str] = None):
    """Reset daily P&L for a subscriber (or all if None)."""
    with _lock:
        if sub_id:
            _daily_pnl.pop(sub_id, None)
        else:
            _daily_pnl.clear()
    log.info(f"Daily P&L reset: {'all' if not sub_id else sub_id}")


# ── Consecutive Loss Streak ──────────────────────────────────────

def get_loss_streak(sub_id: str) -> int:
    """Get current consecutive-loss count for a subscriber."""
    with _lock:
        return _loss_streak.get(sub_id, 0)


def reset_loss_streak(sub_id: Optional[str] = None):
    """
    Reset the consecutive-loss streak for a subscriber (or all if None).
    Called automatically at next-day open OR by admin override.
    """
    with _lock:
        if sub_id:
            _loss_streak.pop(sub_id, None)
        else:
            _loss_streak.clear()
    log.info(f"Loss streak reset: {'all' if not sub_id else sub_id}")


# ── Account Value Cache ──────────────────────────────────────────

def _cached_account_value(sub_id: str) -> Optional[float]:
    """Return cached account value if still fresh, else None."""
    with _lock:
        entry = _account_value_cache.get(sub_id)
        if not entry:
            return None
        age = time.time() - entry["fetched_at"]
        if age > ACCOUNT_VALUE_TTL_SECONDS:
            return None
        return entry["value"]


def _store_account_value(sub_id: str, value: float):
    """Cache an account-value fetch."""
    with _lock:
        _account_value_cache[sub_id] = {"value": float(value), "fetched_at": time.time()}


def get_or_fetch_account_value(sub_id: str, ib) -> Optional[float]:
    """
    Return subscriber's broker NetLiquidation, using a 1-hour cache.

    `ib` is an ib_insync IB() connection. Returns None if the broker call
    fails or returns no NetLiquidation value — caller should treat None as
    "unknown" and decide whether to fail open or closed.
    """
    cached = _cached_account_value(sub_id)
    if cached is not None:
        return cached

    try:
        summary = ib.accountSummary()
        net_liq = next((v.value for v in summary if v.tag == "NetLiquidation"), None)
        if net_liq is None:
            log.warning(f"[{sub_id}] accountSummary() returned no NetLiquidation")
            return None
        value = float(net_liq)
        _store_account_value(sub_id, value)
        log.info(f"[{sub_id}] Account value fetched: ${value:,.2f} (cached {ACCOUNT_VALUE_TTL_SECONDS}s)")
        return value
    except Exception as e:
        log.error(f"[{sub_id}] Failed to fetch account value: {e}")
        return None


# ── Open Position Tracking ───────────────────────────────────────

def record_position_opened(sub_id: str):
    """Increment open position count for a subscriber."""
    with _lock:
        _open_positions[sub_id] = _open_positions.get(sub_id, 0) + 1
        log.info(f"[{sub_id}] Open positions: {_open_positions[sub_id]}")


def record_position_closed(sub_id: str):
    """Decrement open position count for a subscriber."""
    with _lock:
        _open_positions[sub_id] = max(0, _open_positions.get(sub_id, 0) - 1)
        log.info(f"[{sub_id}] Open positions: {_open_positions[sub_id]}")


def get_open_positions(sub_id: str) -> int:
    with _lock:
        return _open_positions.get(sub_id, 0)


# ── Pre-Trade Checks ────────────────────────────────────────────

def check_kill_switch() -> tuple[bool, str]:
    """Check if the global kill switch is engaged."""
    if _kill_switch:
        return False, "BLOCKED: Kill switch is engaged — all trading halted"
    return True, "OK"


def check_daily_loss(sub_id: str) -> tuple[bool, str]:
    """Check if subscriber has exceeded daily loss limit."""
    pnl = get_daily_pnl(sub_id)
    if pnl <= -MAX_DAILY_LOSS:
        return False, f"BLOCKED: Daily loss limit reached ({pnl:.2f} <= -{MAX_DAILY_LOSS})"
    return True, f"OK (daily P&L: {pnl:+.2f}, limit: -{MAX_DAILY_LOSS})"


def check_position_size(quantity: int) -> tuple[bool, str]:
    """Check if order quantity exceeds maximum."""
    if quantity > MAX_POSITION_SIZE:
        return False, f"BLOCKED: Quantity {quantity} exceeds max position size {MAX_POSITION_SIZE}"
    return True, f"OK (quantity: {quantity}, max: {MAX_POSITION_SIZE})"


def check_duplicate_signal(symbol: str, action: str) -> tuple[bool, str]:
    """Check if the same signal was received within cooldown period."""
    key = f"{symbol}:{action}".upper()
    now = time.time()

    with _lock:
        # Clean up expired entries
        expired = [k for k, t in _recent_signals.items() if now - t > COOLDOWN_SECONDS]
        for k in expired:
            del _recent_signals[k]

        last_time = _recent_signals.get(key)
        if last_time and (now - last_time) < COOLDOWN_SECONDS:
            elapsed = now - last_time
            return False, (f"BLOCKED: Duplicate signal {key} — "
                          f"{elapsed:.0f}s ago (cooldown: {COOLDOWN_SECONDS}s)")

        # Record this signal
        _recent_signals[key] = now

    return True, f"OK (signal {key} recorded)"


def check_max_open_positions(sub_id: str) -> tuple[bool, str]:
    """Check if subscriber has too many open positions."""
    count = get_open_positions(sub_id)
    if count >= MAX_OPEN_POSITIONS:
        return False, f"BLOCKED: {count} open positions (max: {MAX_OPEN_POSITIONS})"
    return True, f"OK (open: {count}, max: {MAX_OPEN_POSITIONS})"


def check_trading_hours() -> tuple[bool, str]:
    """Check if current time is within trading hours."""
    now = _now_utc()
    hour = now.hour

    if TRADING_START_HOUR <= hour < TRADING_END_HOUR:
        return True, f"OK (UTC hour: {hour}, window: {TRADING_START_HOUR}-{TRADING_END_HOUR})"
    return False, (f"BLOCKED: Outside trading hours "
                   f"(UTC {hour}:00, window: {TRADING_START_HOUR}:00-{TRADING_END_HOUR}:00)")


def check_notional_value(symbol: str, quantity: int,
                         current_price: Optional[float]) -> tuple[bool, str]:
    """
    Cap $ exposure per trade. Fails open (allows) if price is unknown so
    a missing/zero price doesn't accidentally block legitimate signals —
    position_size still gates raw share count, and daily_loss still caps damage.
    """
    if current_price is None or current_price <= 0:
        return True, f"SKIPPED ({symbol}: no current_price provided)"

    notional = quantity * current_price
    if notional > MAX_NOTIONAL:
        return False, (f"BLOCKED: Notional ${notional:,.2f} "
                       f"({quantity} × ${current_price:.2f}) exceeds max ${MAX_NOTIONAL:,.2f}")
    return True, f"OK (notional: ${notional:,.2f}, max: ${MAX_NOTIONAL:,.2f})"


def check_consecutive_losses(sub_id: str) -> tuple[bool, str]:
    """Block if subscriber has hit the consecutive-loss limit."""
    streak = get_loss_streak(sub_id)
    if streak >= MAX_CONSECUTIVE_LOSSES:
        return False, (f"BLOCKED: {streak} consecutive losses "
                       f"(limit: {MAX_CONSECUTIVE_LOSSES}) — auto-pause; "
                       f"resets at next-day open or admin override")
    return True, f"OK (loss streak: {streak}, limit: {MAX_CONSECUTIVE_LOSSES})"


def check_account_size(sub_id: str, account_value: Optional[float]) -> tuple[bool, str]:
    """
    Block if subscriber's broker NetLiquidation is below the configured minimum.

    When MIN_ACCOUNT_VALUE is 0 (default), this check is disabled — the broker's
    own minimums (PDT, margin, Reg-T) apply. The function returns True without
    consulting the broker so a fetch failure does not block trading.

    When MIN_ACCOUNT_VALUE > 0 the gate is active:
      • account_value below the configured minimum → BLOCKED
      • account_value is None (broker fetch failed) → fails CLOSED, blocked
        (better to pause than to place orders into an unverified account)
    """
    if MIN_ACCOUNT_VALUE <= 0:
        return True, "SKIPPED: account size check disabled (MIN_ACCOUNT_VALUE=0)"
    if account_value is None:
        return False, (f"BLOCKED: Could not verify account value "
                       f"(broker fetch failed). Min required: ${MIN_ACCOUNT_VALUE:,.2f}")
    if account_value < MIN_ACCOUNT_VALUE:
        return False, (f"BLOCKED: Account value ${account_value:,.2f} "
                       f"below minimum ${MIN_ACCOUNT_VALUE:,.2f}")
    return True, f"OK (account value: ${account_value:,.2f}, min: ${MIN_ACCOUNT_VALUE:,.2f})"


# ── Master Gate: Run All Checks ──────────────────────────────────

def pre_trade_check(sub_id: str, symbol: str, action: str,
                    quantity: int,
                    current_price: Optional[float] = None) -> tuple[bool, list[dict]]:
    """
    Run all pre-trade risk checks that DO NOT require a broker connection.

    The account-size check is intentionally NOT here — it needs broker access
    and runs separately via pre_trade_broker_check(sub_id, ib) after the
    connection is established.

    Args:
        sub_id:         subscriber identifier
        symbol:         ticker
        action:         "buy" or "sell"
        quantity:       order size (shares/contracts)
        current_price:  current market price (from the signal payload). If
                        provided, enables the notional-dollar-cap check. If
                        omitted, the notional check is SKIPPED (fails open).

    Returns:
        (allowed, results) where results is a list of
        {"check": name, "passed": bool, "message": str}
    """
    checks = [
        ("kill_switch",         check_kill_switch()),
        ("trading_hours",       check_trading_hours()),
        ("daily_loss",          check_daily_loss(sub_id)),
        ("consecutive_losses",  check_consecutive_losses(sub_id)),
        ("position_size",       check_position_size(quantity)),
        ("notional_value",      check_notional_value(symbol, quantity, current_price)),
        ("duplicate_signal",    check_duplicate_signal(symbol, action)),
        ("max_open_positions",  check_max_open_positions(sub_id)),
    ]

    results = []
    all_passed = True

    for name, (passed, message) in checks:
        results.append({"check": name, "passed": passed, "message": message})
        if not passed:
            all_passed = False
            log.warning(f"[{sub_id}] Risk check FAILED: {name} — {message}")

    if all_passed:
        log.info(f"[{sub_id}] Pre-broker risk checks passed for {action.upper()} {quantity} {symbol}")
    else:
        failed = [r["check"] for r in results if not r["passed"]]
        log.warning(f"[{sub_id}] Trade BLOCKED by: {', '.join(failed)}")

    return all_passed, results


def account_size_gate_active() -> bool:
    """
    True when the account-size minimum gate is enabled (MIN_ACCOUNT_VALUE > 0).

    Lets a broker adapter decide whether it needs to fetch the subscriber's
    account equity before calling pre_trade_broker_check(). When False, callers
    can skip the (broker-specific) equity fetch entirely.
    """
    return MIN_ACCOUNT_VALUE > 0


def pre_trade_broker_check(sub_id: str, ib=None,
                           account_value: Optional[float] = None) -> tuple[bool, list[dict]]:
    """
    Broker-aware risk checks that run AFTER a broker connection is available.

    Currently runs:
      - account_size  (uses cached NetLiquidation / equity; refreshes hourly)

    Broker-agnostic by design:
      - IBKR path passes the ib_insync connection as `ib`; equity is fetched via
        ib.accountSummary() inside get_or_fetch_account_value().
      - Alpaca (and any REST broker) passes the already-fetched equity directly
        as `account_value`, since it has no ib_insync connection. When
        `account_value` is provided it is used as-is and `ib` is ignored.

    When MIN_ACCOUNT_VALUE is 0 (the default), the gate is disabled and we skip
    the equity fetch entirely — no broker call of any kind.

    Returns:
        (allowed, results) — same shape as pre_trade_check()
    """
    # Fast path: when no broker-aware gates are active, skip the broker call.
    if MIN_ACCOUNT_VALUE <= 0:
        log.debug(f"[{sub_id}] Broker risk checks skipped (no active gates)")
        return True, [{"check": "account_size", "passed": True,
                       "message": "SKIPPED: account size check disabled (MIN_ACCOUNT_VALUE=0)"}]

    # Gate active: prefer a caller-supplied equity (REST brokers like Alpaca),
    # otherwise fetch via the ib_insync connection (IBKR).
    if account_value is None:
        account_value = get_or_fetch_account_value(sub_id, ib)
    checks = [
        ("account_size", check_account_size(sub_id, account_value)),
    ]

    results = []
    all_passed = True
    for name, (passed, message) in checks:
        results.append({"check": name, "passed": passed, "message": message})
        if not passed:
            all_passed = False
            log.warning(f"[{sub_id}] Broker risk check FAILED: {name} — {message}")

    if all_passed:
        log.info(f"[{sub_id}] Broker risk checks passed")
    return all_passed, results


# ── Status Report ────────────────────────────────────────────────

def risk_status() -> dict:
    """Return current risk control state for admin dashboard."""
    with _lock:
        now = time.time()
        account_cache_view = {
            sub_id: {
                "value": entry["value"],
                "age_seconds": int(now - entry["fetched_at"]),
                "fresh": (now - entry["fetched_at"]) <= ACCOUNT_VALUE_TTL_SECONDS,
            }
            for sub_id, entry in _account_value_cache.items()
        }
        return {
            "kill_switch": _kill_switch,
            "config": {
                "max_daily_loss": MAX_DAILY_LOSS,
                "max_position_size": MAX_POSITION_SIZE,
                "cooldown_seconds": COOLDOWN_SECONDS,
                "max_open_positions": MAX_OPEN_POSITIONS,
                "trading_hours_utc": f"{TRADING_START_HOUR}:00-{TRADING_END_HOUR}:00",
                "max_notional": MAX_NOTIONAL,
                "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
                "min_account_value": MIN_ACCOUNT_VALUE,
                "account_value_ttl_seconds": ACCOUNT_VALUE_TTL_SECONDS,
            },
            "daily_pnl": {
                sub_id: entry
                for sub_id, entry in _daily_pnl.items()
                if entry.get("date") == _today()
            },
            "open_positions": dict(_open_positions),
            "loss_streaks": dict(_loss_streak),
            "account_value_cache": account_cache_view,
            "recent_signals": {
                k: time.time() - t
                for k, t in _recent_signals.items()
            },
        }
