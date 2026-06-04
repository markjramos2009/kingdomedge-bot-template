"""
KingdomEdge Algo — Self-Hosted Trading Bot Template
====================================================

Single-tenant Flask service that receives TradingView webhook alerts from
the SETS Trade KingdomEdge indicator and places bracket orders on YOUR
Alpaca paper/live account.

This is the deployable scaffold for KingdomEdge Algo Ultimate-tier
subscribers. Fork the repo, deploy to YOUR Railway (or any container host),
fill in your env vars, and the bot runs in your name with your broker keys.
KingdomEdge never sees your keys.

Architecture:
  TradingView Alert → POST /webhook (this server, on YOUR Railway)
  → pre-trade risk checks → submit Alpaca bracket order
  → log to YOUR Railway logs / Sentry (optional)

Compared to the multi-tenant cloud bot:
  - NO subscribers.enc / no encrypted store
  - NO multi-tenant fan-out (one bot = one subscriber)
  - NO admin endpoints (you run this; you don't manage others)
  - NO Stripe / email / TradingView grant integration
    (those are KingdomEdge's responsibility on the central infrastructure)

Required environment variables:
  WEBHOOK_SECRET        — shared secret. MUST match the secret you put in
                          your TradingView alert JSON body. Generate any
                          random 32+ char string and keep it private.
  ALPACA_API_KEY        — your Alpaca API key (paper or live)
  ALPACA_API_SECRET     — your Alpaca API secret
  ALPACA_PAPER          — "true" for paper trading (default), "false" for live

Optional environment variables:
  PORT                  — server port (Railway sets this automatically)
  QUANTITY_OVERRIDE     — fixed contract count per trade (overrides signal qty)
  SENTRY_DSN            — Sentry DSN for error tracking
  SENTRY_ENVIRONMENT    — Sentry environment tag (default: production)

Risk control variables (all optional, sensible defaults):
  RISK_MAX_DAILY_LOSS       — halt after this USD loss per day (default: 500)
  RISK_MAX_POSITION_SIZE    — max contracts/shares per order (default: 10)
  RISK_COOLDOWN_SECONDS     — duplicate signal cooldown in seconds (default: 60)
  RISK_MAX_OPEN_POSITIONS   — max concurrent open positions (default: 5)
  RISK_TRADING_START_HOUR   — market open hour UTC (default: 13 = 9 AM ET)
  RISK_TRADING_END_HOUR     — market close hour UTC (default: 20 = 4 PM ET)
"""

import json
import logging
import os
import uuid

from flask import Flask, request, jsonify
from waitress import serve

import alpaca_broker
from risk_controls import (
    pre_trade_check,
    pre_trade_broker_check,
    record_position_opened,
    risk_status,
    engage_kill_switch,
    disengage_kill_switch,
    is_kill_switch_on,
    account_size_gate_active,
)

# ── Sentry (optional) ─────────────────────────────────────────
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            send_default_pii=True,
            traces_sample_rate=0.5,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        )
    except ImportError:
        pass  # sentry-sdk not installed; silently skip

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY", "")
ALPACA_API_SECRET  = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_PAPER       = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
QUANTITY_OVERRIDE  = int(os.environ.get("QUANTITY_OVERRIDE", "0")) or None
PORT               = int(os.environ.get("PORT", 5000))

# Hardcoded single-tenant sub_id used by risk_controls.py.
# (The risk module is multi-tenant by design; we just always pass "self".)
SUB_ID = "self"

# ── Startup validation ────────────────────────────────────────
_missing = []
if not WEBHOOK_SECRET:    _missing.append("WEBHOOK_SECRET")
if not ALPACA_API_KEY:    _missing.append("ALPACA_API_KEY")
if not ALPACA_API_SECRET: _missing.append("ALPACA_API_SECRET")

_is_production = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
if _missing:
    msg = f"Missing required env vars: {', '.join(_missing)}"
    if _is_production:
        raise RuntimeError(msg + " (production startup blocked)")
    else:
        log.warning(msg + " (bot will reject all webhook requests until set)")
else:
    log.info(f"Configured for Alpaca {'paper' if ALPACA_PAPER else 'LIVE'} trading")
    if QUANTITY_OVERRIDE:
        log.info(f"QUANTITY_OVERRIDE active: every signal traded as {QUANTITY_OVERRIDE} units")

# ── Flask app ─────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/ping", methods=["GET"])
def ping():
    """Simple liveness check. Returns 200 if the process is up."""
    return jsonify({"status": "ok", "service": "kingdomedge-bot-template"}), 200


@app.route("/status", methods=["GET"])
def status():
    """Status check including risk-control state + Alpaca connectivity."""
    rs = risk_status()
    return jsonify({
        "status": "ok",
        "service": "kingdomedge-bot-template",
        "alpaca_paper": ALPACA_PAPER,
        "quantity_override": QUANTITY_OVERRIDE,
        "risk": rs,
        "kill_switch": is_kill_switch_on(),
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receive a TradingView alert JSON body and execute the bracket trade
    on Alpaca. Expected JSON:
      {
        "secret": "<WEBHOOK_SECRET>",
        "action": "buy" | "sell",
        "ticker": "SOXL",
        "quantity": 1,
        "sl":  123.45,
        "tp1": 130.00,
        "tp2": 132.00,    (optional)
        "tp3": 134.00,    (optional)
        "event": "sets"   (label, ignored)
      }
    """
    # ── 1. Parse + auth ───────────────────────────────────────
    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        log.warning(f"Webhook: bad JSON: {e}")
        return jsonify({"error": "Bad JSON"}), 400

    if not WEBHOOK_SECRET or data.get("secret") != WEBHOOK_SECRET:
        log.warning("Webhook: invalid or missing secret")
        return jsonify({"error": "Unauthorized"}), 401

    action   = (data.get("action") or "").upper()
    ticker   = (data.get("ticker") or "").upper()
    quantity = int(QUANTITY_OVERRIDE or data.get("quantity", 0) or 0)
    sl       = data.get("sl")
    tp1      = data.get("tp1")
    tp2      = data.get("tp2")
    tp3      = data.get("tp3")

    log.info(f"Signal received: {action} {quantity} {ticker} SL={sl} TP1={tp1} TP2={tp2} TP3={tp3}")

    if action not in ("BUY", "SELL"):
        return jsonify({"error": f"Invalid action: {action}"}), 400
    if not ticker or quantity <= 0 or sl is None or tp1 is None:
        return jsonify({"error": "Missing required fields (ticker, quantity, sl, tp1)"}), 400

    # ── 2. Pre-trade risk checks ──────────────────────────────
    risk_ok, risk_reason = pre_trade_check(
        sub_id=SUB_ID, symbol=ticker, action=action, quantity=quantity
    )
    if not risk_ok:
        log.warning(f"Pre-trade check blocked signal: {risk_reason}")
        return jsonify({"status": "blocked", "reason": risk_reason}), 200

    # Broker-side pre-trade check (e.g. account size)
    broker_ok, broker_reason = pre_trade_broker_check(sub_id=SUB_ID)
    if not broker_ok:
        log.warning(f"Broker pre-trade check blocked: {broker_reason}")
        return jsonify({"status": "blocked", "reason": broker_reason}), 200

    # ── 3. Submit bracket order via Alpaca ────────────────────
    try:
        result = alpaca_broker.submit_bracket_orders(
            api_key=ALPACA_API_KEY,
            api_secret=ALPACA_API_SECRET,
            symbol=ticker,
            action=action,
            quantity=quantity,
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2) if tp2 is not None else None,
            tp3=float(tp3) if tp3 is not None else None,
            paper=ALPACA_PAPER,
            sub_id=SUB_ID,
        )
    except Exception as e:
        log.error(f"Alpaca order submission failed: {e}")
        if SENTRY_DSN:
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(e)
            except ImportError:
                pass
        return jsonify({"error": "Order submission failed", "detail": str(e)}), 502

    # ── 4. Record + return ────────────────────────────────────
    record_position_opened(SUB_ID)
    log.info(f"Bracket order placed: {result}")
    return jsonify({
        "status": "placed",
        "action": action,
        "ticker": ticker,
        "quantity": quantity,
        "sl": sl,
        "tps": [tp for tp in (tp1, tp2, tp3) if tp is not None],
        "broker_response": result,
    }), 200


@app.route("/kill", methods=["POST"])
def kill():
    """Emergency kill switch — engage / disengage."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    if data.get("action") == "engage":
        engage_kill_switch(reason=data.get("reason", "manual"))
        log.warning(f"Kill switch ENGAGED: {data.get('reason', 'manual')}")
        return jsonify({"status": "engaged"}), 200
    elif data.get("action") == "disengage":
        disengage_kill_switch()
        log.info("Kill switch DISENGAGED")
        return jsonify({"status": "disengaged"}), 200
    else:
        return jsonify({"error": "action must be 'engage' or 'disengage'"}), 400

# -- Run -------------------------------------------------------
if __name__ == "__main__":
    log.info(f"Starting KingdomEdge Bot Template on port {PORT}...")
    serve(app, host="0.0.0.0", port=PORT)
