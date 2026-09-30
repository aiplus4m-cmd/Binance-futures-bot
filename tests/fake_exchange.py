"""In-memory stand-in for BinanceFutures used by the simulation tests."""
import itertools
import time
from decimal import Decimal

from bot import config as C
from bot.binance_client import BinanceError, SymbolInfo


class FakeExchange:
    def __init__(self, wallet=1000.0):
        self.wallet = wallet
        self.symbols = {"ETHUSDT": SymbolInfo("ETHUSDT", "ETH", Decimal("0.01"), Decimal("0.001"),
                                              Decimal("0.001"), Decimal("10000"), 5.0, 0)}
        self.mark = {"ETHUSDT": 100.0}
        self.pos = {}           # symbol -> {"amt", "entry"}
        self.fills = []
        self.incomes = []
        self.triggers = {}      # id -> dict
        self.ids = itertools.count(1)
        self.klines_by_tf = {}
        self.leverage = {}

    # ---------------------------------------------------------- helpers for tests
    def set_price(self, sym, price):
        self.mark[sym] = price

    def fire_triggers(self, sym):
        """Simulate exchange-side SL/TP execution at the current mark."""
        p = self.pos.get(sym)
        if not p:
            return None
        m = self.mark[sym]
        for tid, t in list(self.triggers.items()):
            if t["symbol"] != sym:
                continue
            hit = (m <= t["price"]) if (t["side"] == "SELL") == (t["type"] == "STOP_MARKET") else (m >= t["price"])
            if hit:
                self._fill(sym, t["side"], abs(p["amt"]), m)
                del self.triggers[tid]
                return t["type"]
        return None

    def _now(self):
        return int(time.time() * 1000)

    def _fill(self, sym, side, qty, price):
        p = self.pos.setdefault(sym, {"amt": 0.0, "entry": 0.0})
        signed = qty if side == "BUY" else -qty
        realized = 0.0
        if p["amt"] == 0 or (p["amt"] > 0) == (signed > 0):
            new_amt = p["amt"] + signed
            p["entry"] = (p["entry"] * abs(p["amt"]) + price * qty) / abs(new_amt)
            p["amt"] = new_amt
        else:
            realized = (price - p["entry"]) * qty * (1 if p["amt"] > 0 else -1)
            p["amt"] += signed
            if abs(p["amt"]) < 1e-12:
                del self.pos[sym]
        fee = qty * price * C.TAKER_FEE
        self.wallet += realized - fee
        self.fills.append({"symbol": sym, "side": side, "qty": str(qty), "price": str(price),
                           "realizedPnl": str(realized), "commission": str(fee), "commissionAsset": "USDT",
                           "time": self._now()})
        return realized

    # ---------------------------------------------------------- client API
    async def mark_price(self, sym):
        return self.mark[sym]

    async def bracket_for(self, sym, notional):
        return 50, 0.01

    async def set_isolated(self, sym):
        pass

    async def set_leverage(self, sym, lev):
        self.leverage[sym] = lev

    async def market_order(self, sym, side, qty, reduce_only=False):
        qty = self.symbols[sym].round_qty(qty)
        if reduce_only and sym not in self.pos:
            raise BinanceError(400, -2022, "ReduceOnly Order is rejected")
        self._fill(sym, side, qty, self.mark[sym])
        return {"executedQty": str(qty), "avgPrice": str(self.mark[sym])}

    async def place_close_trigger(self, sym, side, otype, trigger):
        price = self.symbols[sym].round_price(trigger)
        m = self.mark[sym]
        immediate = (m <= price) if (side == "SELL") == (otype == "STOP_MARKET") else (m >= price)
        if immediate:
            raise BinanceError(400, -2021, "Order would immediately trigger.")
        oid = next(self.ids)
        self.triggers[oid] = {"symbol": sym, "side": side, "type": otype, "price": price}
        return {"kind": "algo", "id": oid, "price": price}

    async def cancel_trigger(self, sym, ref):
        if ref:
            self.triggers.pop(ref["id"], None)

    async def open_trigger_ids(self, sym):
        return {("algo", i) for i, t in self.triggers.items() if t["symbol"] == sym}

    async def cancel_all(self, sym):
        for i in [i for i, t in self.triggers.items() if t["symbol"] == sym]:
            del self.triggers[i]

    def _prow(self, sym):
        p = self.pos[sym]
        m = self.mark[sym]
        return {"symbol": sym, "positionAmt": str(p["amt"]), "entryPrice": str(p["entry"]), "markPrice": str(m),
                "unRealizedProfit": str((m - p["entry"]) * p["amt"]), "liquidationPrice": "0"}

    async def positions(self):
        return {s: self._prow(s) for s in self.pos}

    async def position(self, sym):
        return self._prow(sym) if sym in self.pos else None

    async def user_trades(self, sym, start_ms):
        return [f for f in self.fills if f["symbol"] == sym and f["time"] >= start_ms]

    async def income(self, start_ms, end_ms=None, symbol=None, income_type=None):
        return [r for r in self.incomes if int(r["time"]) >= start_ms
                and (symbol is None or r.get("symbol") == symbol)
                and (income_type is None or r["incomeType"] == income_type)]

    async def account(self):
        upnl = sum((self.mark[s] - p["entry"]) * p["amt"] for s, p in self.pos.items())
        return {"totalWalletBalance": str(self.wallet), "totalMarginBalance": str(self.wallet + upnl),
                "availableBalance": str(self.wallet), "totalUnrealizedProfit": str(upnl)}

    async def klines(self, sym, interval, limit=150):
        rows = self.klines_by_tf.get(interval)
        if rows is None:  # flat default
            rows = [[0, 100, 101, 99, 100, 1000] for _ in range(limit + 1)]
        return rows
