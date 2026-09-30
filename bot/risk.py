"""Pure position math: sizing, leverage, TP/SL, DCA, break-even, trailing, time exits.

Kept free of I/O so it can be unit-tested.
"""
import math
from dataclasses import dataclass

from . import config as C


def sign(side: str) -> int:
    return 1 if side == "LONG" else -1


@dataclass
class Plan:
    side: str
    entry: float
    sl: float
    tp: float
    tp1: float
    qty: float
    leverage: int
    target_usd: float
    risk_usd: float
    expected_usd: float
    rr: float

    @property
    def notional(self) -> float:
        return self.qty * self.entry


def stop_distance(side: str, entry: float, atr: float, swing: float | None) -> float | None:
    """SL distance: beyond the recent swing, but at least SL_ATR_MULT * ATR."""
    s = sign(side)
    dist = C.SL_ATR_MULT * atr
    if swing is not None and s * (entry - swing) > 0:
        dist = max(dist, abs(entry - swing) + C.SL_SWING_BUFFER_ATR * atr)
    if dist > C.MAX_SL_ATR_MULT * atr:
        return None
    dist = max(dist, C.MIN_SL_PCT * entry)
    if dist > C.MAX_SL_PCT * entry:
        return None
    return dist


def targets(side: str, avg: float, qty: float, target_usd: float) -> tuple[float, float]:
    """(tp1, tp) so that closing `qty` at tp nets `target_usd` after fees."""
    s = sign(side)
    d = target_usd / qty + avg * C.TAKER_FEE * 2
    return avg + s * d * C.TP1_DISTANCE_RATIO, avg + s * d


def safe_leverage(sl_pct: float, mmr: float, bracket_max: int) -> int:
    """Highest leverage whose liquidation stays LIQ_SAFETY x further than the SL."""
    lev = math.floor(1 / (C.LIQ_SAFETY * sl_pct + mmr))
    return max(1, min(lev, C.MAX_LEVERAGE, bracket_max))


def plan_trade(side: str, entry: float, atr: float, swing: float | None, capital: float,
               bracket_max: int = 125, mmr: float = 0.01) -> tuple[Plan | None, str]:
    s = sign(side)
    sl_dist = stop_distance(side, entry, atr, swing)
    if sl_dist is None:
        return None, "SL quá xa (cấu trúc/biến động lớn)"
    tp_dist = max(C.MIN_RR * sl_dist, C.TP_ATR_MULT * atr)
    if tp_dist / entry > C.MAX_TP_PCT:
        return None, "TP quá xa"

    fee_unit = entry * C.TAKER_FEE * 2
    target = capital * C.TARGET_PROFIT_PCT
    qty = target / (tp_dist - fee_unit)
    # cap the initial risk, keeping part of the budget for one DCA
    max_initial_risk = capital * C.MAX_RISK_PCT * (0.8 if C.DCA_MAX else 1.0)
    qty = min(qty, max_initial_risk / (sl_dist + fee_unit))

    sl_pct = sl_dist / entry
    lev_cap = safe_leverage(sl_pct, mmr, bracket_max)
    dca_room = 1 + (C.DCA_MAX_RATIO if C.DCA_MAX else 0)
    max_notional = capital * C.MAX_MARGIN_PCT * lev_cap / dca_room
    qty = min(qty, max_notional / entry)
    notional = qty * entry
    leverage = min(lev_cap, max(1, math.ceil(notional * dca_room / (capital * C.MAX_MARGIN_PCT))))

    expected = qty * (tp_dist - fee_unit)
    if expected < capital * C.MIN_EXPECTED_PROFIT_PCT:
        return None, f"lợi nhuận kỳ vọng {expected / capital:.2%} < {C.MIN_EXPECTED_PROFIT_PCT:.1%} vốn"

    tp1, tp = targets(side, entry, qty, expected)
    plan = Plan(side=side, entry=entry, sl=entry - s * sl_dist, tp=tp, tp1=tp1, qty=qty, leverage=leverage,
                target_usd=expected, risk_usd=qty * (sl_dist + fee_unit), expected_usd=expected,
                rr=tp_dist / sl_dist)
    return plan, "ok"


def adverse_fraction(side: str, avg: float, sl: float, price: float) -> float:
    """0 at avg, 1 at SL (how far price travelled toward the stop)."""
    return sign(side) * (avg - price) / abs(avg - sl)


def dca_qty(side: str, avg: float, qty: float, sl: float, price: float, capital: float,
            initial_qty: float) -> float:
    """Size of an add that keeps total loss-at-SL within MAX_RISK_PCT."""
    fee = C.TAKER_FEE * 2
    risk_now = qty * (abs(avg - sl) + avg * fee)
    budget = capital * C.MAX_RISK_PCT - risk_now
    per_unit = abs(price - sl) + price * fee
    if budget <= 0 or per_unit <= 0:
        return 0.0
    return max(0.0, min(initial_qty * C.DCA_MAX_RATIO, budget / per_unit))


def new_average(avg: float, qty: float, price: float, add: float) -> float:
    return (avg * qty + price * add) / (qty + add)


def breakeven_price(side: str, avg: float) -> float:
    return avg * (1 + sign(side) * (2 * C.TAKER_FEE + C.BE_BUFFER_PCT))


def trailing_stop(side: str, extreme: float, atr: float, current_sl: float, avg: float) -> float | None:
    """New (tighter) SL after TP1, never below break-even; None if not worth moving."""
    s = sign(side)
    cand = extreme - s * C.TRAIL_ATR_MULT * atr
    be = breakeven_price(side, avg)
    if s * (cand - be) < 0:
        cand = be
    if s * (cand - current_sl) >= C.TRAIL_MIN_STEP_ATR * atr:
        return cand
    return None


def required_exit_profit(age_hours: float, target_usd: float, capital: float) -> float | None:
    """Minimum net PnL (USDT) to close an old position; None = too young for a time exit.

    Starts at 80% of the target after STALE_HOURS and decays linearly to a small
    positive floor at MAX_HOLD_HOURS. Never below zero: losers are only closed by SL.
    """
    floor = capital * C.MIN_EXIT_PROFIT_PCT
    if age_hours < C.STALE_HOURS:
        return None
    if age_hours >= C.MAX_HOLD_HOURS:
        return floor
    frac = (age_hours - C.STALE_HOURS) / (C.MAX_HOLD_HOURS - C.STALE_HOURS)
    return max(floor, 0.8 * target_usd * (1 - frac))


def crossed(side: str, price: float, level: float) -> bool:
    """Price reached a profit level (>= for long, <= for short)."""
    return sign(side) * (price - level) >= 0
