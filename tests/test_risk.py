import pytest

from bot import config as C
from bot import risk


def test_plan_long_targets_capital_percentage():
    plan, why = risk.plan_trade("LONG", entry=100.0, atr=1.0, swing=98.8, capital=1000)
    assert plan, why
    assert plan.sl < plan.entry < plan.tp1 < plan.tp
    # full TP nets between 1.5% and 2% of capital after fees
    assert C.MIN_EXPECTED_PROFIT_PCT * 1000 <= plan.expected_usd <= C.TARGET_PROFIT_PCT * 1000 + 1e-9
    net_at_tp = plan.qty * (plan.tp - plan.entry) - plan.qty * plan.entry * C.TAKER_FEE * 2
    assert net_at_tp == pytest.approx(plan.expected_usd, rel=1e-6)
    # risk capped below the total risk budget (room kept for DCA)
    assert plan.risk_usd <= 1000 * C.MAX_RISK_PCT
    assert plan.rr >= C.MIN_RR - 1e-9
    # partial at TP1 + rest at TP still >= 1.5% of capital
    realized = plan.expected_usd * (C.TP1_CLOSE_RATIO * C.TP1_DISTANCE_RATIO + (1 - C.TP1_CLOSE_RATIO))
    assert realized >= 0.015 * 1000 * 0.99


def test_plan_short_mirrors():
    plan, _ = risk.plan_trade("SHORT", entry=100.0, atr=1.0, swing=101.0, capital=1000)
    assert plan.tp < plan.tp1 < plan.entry < plan.sl


def test_leverage_keeps_liquidation_far_from_sl():
    plan, _ = risk.plan_trade("LONG", entry=100.0, atr=1.5, swing=None, capital=1000, mmr=0.01)
    sl_pct = (plan.entry - plan.sl) / plan.entry
    liq_pct = 1 / plan.leverage - 0.01
    assert liq_pct >= C.LIQ_SAFETY * sl_pct
    assert plan.leverage <= C.MAX_LEVERAGE
    # margin incl. DCA room fits the per-position budget
    assert plan.notional * (1 + C.DCA_MAX_RATIO) / plan.leverage <= 1000 * C.MAX_MARGIN_PCT + 1e-6


def test_rejects_far_structure_and_low_bracket():
    plan, _ = risk.plan_trade("LONG", entry=100.0, atr=1.0, swing=95.0, capital=1000)
    assert plan is None
    # exchange only allows 1x -> expected profit too small -> skip
    plan, why = risk.plan_trade("LONG", entry=100.0, atr=0.5, swing=None, capital=1000, bracket_max=1)
    assert plan is None and "kỳ vọng" in why


def test_dca_respects_total_risk_and_improves_average():
    plan, _ = risk.plan_trade("LONG", entry=100.0, atr=1.0, swing=None, capital=1000)
    price = plan.entry - 0.5 * (plan.entry - plan.sl)
    add = risk.dca_qty("LONG", plan.entry, plan.qty, plan.sl, price, 1000, plan.qty)
    assert 0 < add <= plan.qty * C.DCA_MAX_RATIO
    avg = risk.new_average(plan.entry, plan.qty, price, add)
    assert price < avg < plan.entry
    q = plan.qty + add
    loss_at_sl = q * (avg - plan.sl) + q * avg * C.TAKER_FEE * 2
    assert loss_at_sl <= 1000 * C.MAX_RISK_PCT + 1e-6
    tp1, tp = risk.targets("LONG", avg, q, plan.target_usd)
    assert tp < plan.tp  # bigger size -> closer TP, same USD target


def test_breakeven_and_trailing_never_below_be():
    be = risk.breakeven_price("LONG", 100)
    assert be > 100 * (1 + 2 * C.TAKER_FEE)
    # extreme barely above avg -> trail clamps to BE
    assert risk.trailing_stop("LONG", 100.5, 1.0, current_sl=97, avg=100) == pytest.approx(be)
    # strong move -> trail follows extreme
    assert risk.trailing_stop("LONG", 105, 1.0, current_sl=be, avg=100) == pytest.approx(105 - C.TRAIL_ATR_MULT)
    # tiny improvement -> no move
    assert risk.trailing_stop("LONG", 105.1, 1.0, current_sl=105 - C.TRAIL_ATR_MULT, avg=100) is None
    assert risk.trailing_stop("SHORT", 95, 1.0, current_sl=103, avg=100) == pytest.approx(95 + C.TRAIL_ATR_MULT)


def test_required_exit_profit_decays_but_stays_positive():
    assert risk.required_exit_profit(C.STALE_HOURS - 1, 20, 1000) is None
    start = risk.required_exit_profit(C.STALE_HOURS, 20, 1000)
    mid = risk.required_exit_profit((C.STALE_HOURS + C.MAX_HOLD_HOURS) / 2, 20, 1000)
    end = risk.required_exit_profit(C.MAX_HOLD_HOURS + 10, 20, 1000)
    assert start > mid > end > 0
    assert end == pytest.approx(1000 * C.MIN_EXIT_PROFIT_PCT)


def test_adverse_fraction():
    assert risk.adverse_fraction("LONG", 100, 98, 99) == pytest.approx(0.5)
    assert risk.adverse_fraction("SHORT", 100, 102, 101) == pytest.approx(0.5)
    assert risk.adverse_fraction("LONG", 100, 98, 101) < 0
