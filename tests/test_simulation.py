"""End-to-end position lifecycle against the in-memory exchange."""
import asyncio

import pytest

from bot import config as C
from bot import risk
from bot.accounting import Accounting
from bot.db import DB, now_ms
from bot.strategy import Scanner, Signal
from bot.trader import Trader
from tests.fake_exchange import FakeExchange

SYM = "ETHUSDT"


def rising_klines(n=160, start=80.0, step=0.15):
    rows, c = [], start
    for i in range(n):
        o = c
        c = c + step if i % 3 else c - step * 0.5
        rows.append([0, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 1000])
    # last closed candle (index -2) is a strong green bounce; index -1 is the forming candle
    o = rows[-2][4]
    rows[-2] = [0, o - 0.6, o + 0.1, o - 0.7, o, 1500]
    return rows


@pytest.fixture
def env(tmp_path, monkeypatch):
    async def no_sleep(*_):
        return None
    monkeypatch.setattr("bot.trader.asyncio.sleep", no_sleep)
    ex = FakeExchange(1000.0)
    db = DB(str(tmp_path / "t.db"))
    acct = Accounting(ex, db)
    msgs = []

    async def notify(m):
        msgs.append(m)
    trader = Trader(ex, db, acct, Scanner(ex), notify)
    asyncio.run(acct.init_capital())
    return ex, db, acct, trader, msgs


def open_long(ex, trader, acct):
    sig = Signal(SYM, "LONG", 80, 100.0, atr=1.0, swing=98.8, reasons=["test"])
    ok, msg = asyncio.run(trader.open_trade(sig, asyncio.run(acct.balances())))
    assert ok, msg
    return trader.db.open_trades()[0]


def test_open_places_sl_and_tp(env):
    ex, db, acct, trader, msgs = env
    t = open_long(ex, trader, acct)
    types = sorted(x["type"] for x in ex.triggers.values())
    assert types == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert t["sl"] < 100 < t["tp1"] < t["tp"]
    assert ex.leverage[SYM] == t["leverage"]
    assert "MỞ LONG" in msgs[-1]


def test_tp1_partial_then_trailing_then_stop_is_profitable(env):
    ex, db, acct, trader, msgs = env
    t = open_long(ex, trader, acct)
    qty0 = t["qty"]
    ex.set_price(SYM, t["tp1"] + 0.01)
    asyncio.run(trader.manage())
    t = db.trade(t["id"])
    assert t["tp1_done"] == 1
    assert ex.pos[SYM]["amt"] == pytest.approx(qty0 - round(qty0 * C.TP1_CLOSE_RATIO, 3), abs=1e-3)
    assert t["sl"] >= risk.breakeven_price("LONG", t["avg_price"]) - 0.01

    # price runs higher -> trailing SL follows
    ex.set_price(SYM, t["tp1"] + 1.2)
    asyncio.run(trader.manage())
    t2 = db.trade(t["id"])
    assert t2["sl"] > t["sl"]

    # reverse into the trailing stop -> exchange closes, bot books a profit
    ex.set_price(SYM, t2["sl"] - 0.05)
    assert ex.fire_triggers(SYM) == "STOP_MARKET"
    asyncio.run(trader.manage())
    t3 = db.trade(t["id"])
    assert t3["status"] == "CLOSED" and t3["pnl"] > 0
    assert t3["close_reason"] in ("TRAILING_SL", "SL")
    assert not ex.triggers


def test_stop_loss_loss_is_bounded(env):
    ex, db, acct, trader, msgs = env
    t = open_long(ex, trader, acct)
    ex.set_price(SYM, t["sl"] - 0.01)
    ex.fire_triggers(SYM)
    asyncio.run(trader.manage())
    t = db.trade(t["id"])
    assert t["status"] == "CLOSED" and t["close_reason"] == "SL"
    assert -1000 * C.MAX_RISK_PCT <= t["pnl"] < 0


def test_losing_position_is_not_closed_by_time_or_manual(env):
    ex, db, acct, trader, msgs = env
    t = open_long(ex, trader, acct)
    db.update_trade(t["id"], opened_at=now_ms() - 60 * 3600_000)
    ex.set_price(SYM, 99.8)
    asyncio.run(trader.manage(slow=True))
    assert db.trade(t["id"])["status"] == "OPEN"
    reply = asyncio.run(trader.manual_close(SYM, force=False))
    assert "đang lỗ" in reply and db.trade(t["id"])["status"] == "OPEN"


def test_old_position_closed_when_net_positive(env):
    ex, db, acct, trader, msgs = env
    t = open_long(ex, trader, acct)
    db.update_trade(t["id"], opened_at=now_ms() - 60 * 3600_000)
    ex.set_price(SYM, 100.3)  # small profit, below TP1
    asyncio.run(trader.manage(slow=True))
    t = db.trade(t["id"])
    assert t["status"] == "CLOSED" and t["close_reason"].startswith("TIME_EXIT") and t["pnl"] > 0


def test_dca_improves_average_keeps_sl(env):
    ex, db, acct, trader, msgs = env
    ex.klines_by_tf = {"1h": rising_klines(), "4h": rising_klines(), "15m": rising_klines()}
    t = open_long(ex, trader, acct)
    price = t["entry_price"] - 0.55 * (t["entry_price"] - t["initial_sl"])
    ex.set_price(SYM, price)
    asyncio.run(trader.manage(slow=True))
    t2 = db.trade(t["id"])
    assert t2["dca_count"] == 1
    assert t2["avg_price"] < t["avg_price"] and t2["qty"] > t["qty"]
    assert t2["sl"] == t["sl"]
    assert t2["tp"] < t["tp"]
    loss_at_sl = t2["qty"] * (t2["avg_price"] - t2["sl"]) * (1 + 0.001)
    assert loss_at_sl <= 1000 * C.MAX_RISK_PCT + 0.5
    tp_orders = [x for x in ex.triggers.values() if x["type"] == "TAKE_PROFIT_MARKET"]
    assert len(tp_orders) == 1 and tp_orders[0]["price"] == pytest.approx(t2["tp"], abs=0.01)


def test_transfers_tracked_in_capital(env):
    ex, db, acct, trader, msgs = env
    now = now_ms()
    ex.incomes += [
        {"tranId": 1, "time": now, "symbol": "", "incomeType": "TRANSFER", "income": "500", "asset": "USDT"},
        {"tranId": 2, "time": now + 1, "symbol": "", "incomeType": "TRANSFER", "income": "-200", "asset": "USDT"},
    ]
    ex.wallet += 300
    new = asyncio.run(acct.sync_income())
    assert len(new) == 2
    assert asyncio.run(acct.sync_income()) == []  # dedup
    cap = asyncio.run(acct.capital_summary())
    assert cap["initial"] == 1000 and cap["deposits"] == 500 and cap["withdrawals"] == 200
    assert cap["invested"] == 1300 and cap["pnl"] == pytest.approx(0)
