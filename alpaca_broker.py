"""
Alpaca REST adapter for the KingdomEdge Algo cloud bot.
=======================================================

Dependency-free: uses urllib only, mirroring the Stripe integration pattern
already in trading_bot_cloud.py (no alpaca-py SDK, keeps the Railway build lean).

Why this exists
---------------
The bot's original broker is IBKR (ib_insync, see connections.py), which requires
reaching each subscriber's local TWS at 127.0.0.1:7497 — impossible from Railway's
container. Alpaca is a stateless REST broker: every order is an HTTPS POST with the
subscriber's API key/secret, so it works for cloud-hosted retail subscribers.

3-take-profit scale-out
-----------------------
The KE methodology scales out across up to 3 take-profits sharing one stop. IBKR
expresses this as a single OCA bracket with 3 TP legs. Alpaca's native bracket
order supports only ONE take-profit + ONE stop-loss, so we replicate the scale-out
by submitting N independent bracket orders (one per TP level), each for a slice of
the quantity, all sharing the same stop price. Behaviour matches the IB path:
  - TP1 fills  -> slice 1 closes, slices 2/3 keep running to their own TPs
  - Stop hits  -> every slice's stop triggers, closing the whole position at the stop

Each Alpaca bracket is itself OCO-managed by Alpaca (if a slice's TP fills, that
slice's stop is auto-cancelled, and vice-versa), so each slice is independently safe.
"""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE  = "https://api.alpaca.markets"


def _base(paper: bool) -> str:
    return PAPER_BASE if paper else LIVE_BASE


def normalize_symbol(symbol: str) -> str:
    """
    Alpaca expects a bare ticker ('SOXL'). TradingView/Pine may send an
    exchange-qualified ticker ('AMEX:SOXL'). Strip any exchange prefix.
    """
    if not symbol:
        return symbol
    return symbol.split(":")[-1].strip().upper()


def _request(method: str, path: str, api_key: str, api_secret: str,
             paper: bool = True, body: dict = None, timeout: int = 10) -> dict:
    """
    Make an authenticated request to the Alpaca REST API.
    Raises RuntimeError with the Alpaca error body on non-2xx.
    """
    url = _base(paper) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("APCA-API-KEY-ID", api_key)
    req.add_header("APCA-API-SECRET-KEY", api_secret)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        log.error(f"Alpaca API error ({e.code}) on {method} {path}: {err_body}")
        raise RuntimeError(f"Alpaca {e.code}: {err_body}")
    except urllib.error.URLError as e:
        log.error(f"Alpaca network error on {method} {path}: {e}")
        raise RuntimeError(f"Alpaca network error: {e}")


# ── Account ───────────────────────────────────────────────────

def get_account(api_key: str, api_secret: str, paper: bool = True) -> dict:
    """
    Return the subscriber's Alpaca account object.
    Useful keys: equity, last_equity, buying_power, cash, status.
    """
    return _request("GET", "/v2/account", api_key, api_secret, paper)


def get_account_value(api_key: str, api_secret: str, paper: bool = True) -> float:
    """
    Return the account's current equity (NetLiquidation equivalent) as a float.
    Used by the risk-control account-size gate when it is enabled.
    """
    acct = get_account(api_key, api_secret, paper)
    try:
        return float(acct.get("equity", 0.0))
    except (TypeError, ValueError):
        return 0.0


# ── Orders ────────────────────────────────────────────────────

def _split_quantities(quantity: int, n: int) -> list[int]:
    """
    Split `quantity` into `n` slices, remainder onto the last slice.
    Mirrors the IB partial-quantity logic in place_bracket_orders().
    """
    base = quantity // n
    rem  = quantity % n
    qtys = [base] * n
    qtys[-1] += rem
    return qtys


def submit_bracket_orders(api_key: str, api_secret: str, symbol: str, action: str,
                          quantity: int, sl: float, tp1: float, tp2: float = None,
                          tp3: float = None, paper: bool = True,
                          sub_id: str = "unknown") -> list[dict]:
    """
    Replicate the IB 3-TP scale-out on Alpaca by submitting one bracket order
    per take-profit level. Each bracket is a market entry + take_profit limit
    + stop_loss stop, for a slice of the total quantity, all sharing `sl`.

    Returns the list of Alpaca order objects created.
    """
    sym  = normalize_symbol(symbol)
    side = action.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid action: {action}")

    tps = [tp for tp in (tp1, tp2, tp3) if tp is not None]
    n   = len(tps)
    if n == 0:
        raise ValueError("submit_bracket_orders requires at least one take-profit")

    # Not enough shares to split across all TPs -> collapse to a single TP1 bracket.
    if quantity < n:
        tps = tps[:1]
        n = 1
    qtys = _split_quantities(quantity, n)

    placed = []
    for i, (tp, qty) in enumerate(zip(tps, qtys), start=1):
        if qty <= 0:
            continue
        body = {
            "symbol":        sym,
            "qty":           str(qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",
            "order_class":   "bracket",
            "take_profit":   {"limit_price": round(float(tp), 2)},
            "stop_loss":     {"stop_price":  round(float(sl), 2)},
        }
        order = _request("POST", "/v2/orders", api_key, api_secret, paper, body)
        placed.append(order)
        log.info(f"[{sub_id}] Alpaca bracket {i}/{n}: {side.upper()} {qty} {sym}  "
                 f"TP={tp}  SL={sl}  id={order.get('id', '?')}")

    log.info(f"[{sub_id}] Alpaca bracket scale-out submitted: {len(placed)} legs "
             f"({side.upper()} {quantity} {sym}  SL={sl}  TP1={tp1} TP2={tp2} TP3={tp3})")
    return placed


def submit_market_order(api_key: str, api_secret: str, symbol: str, action: str,
                        quantity: int, paper: bool = True,
                        sub_id: str = "unknown") -> dict:
    """
    Submit a simple market order with no bracket (used when a signal arrives
    without SL/TP). Mirrors the IB simple-order fallback in the webhook handler.
    """
    sym  = normalize_symbol(symbol)
    side = action.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid action: {action}")
    body = {
        "symbol":        sym,
        "qty":           str(quantity),
        "side":          side,
        "type":          "market",
        "time_in_force": "gtc",
    }
    order = _request("POST", "/v2/orders", api_key, api_secret, paper, body)
    log.info(f"[{sub_id}] Alpaca market order: {side.upper()} {quantity} {sym}  "
             f"id={order.get('id', '?')}")
    return order


def validate_credentials(api_key: str, api_secret: str, paper: bool = True) -> dict:
    """
    Lightweight check that a subscriber's keys work and the account is active.
    Returns {"ok": bool, "status": str, "equity": float, "error": str|None}.
    Used during onboarding / setup-call verification before going live.
    """
    try:
        acct = get_account(api_key, api_secret, paper)
        return {
            "ok": acct.get("status") == "ACTIVE",
            "status": acct.get("status"),
            "equity": float(acct.get("equity", 0.0)),
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "status": None, "equity": 0.0, "error": str(e)}
