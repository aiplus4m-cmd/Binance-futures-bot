"""Order execution and position management."""
import asyncio
import logging
import time

from . import config as C
from . import risk
from .accounting import today, usd
from .binance_client import BinanceError
from .db import now_ms
from .strategy import Signal, bounce_confirmed, trend_intact, trend_reversed

log = logging.getLogger(__name__)


def open_side(side: str) -> str:
    return "BUY" if side == "LONG" else "SELL"


def close_side(side: str) -> str:
    return "SELL" if side == "LONG" else "BUY"


class Trader:
    def __init__(self, client, db, acct, scanner, notify):
        self.client, self.db, self.acct, self.scanner = client, db, acct, scanner
        self.notify = notify
        self._lock = asyncio.Lock()          # serialises order actions
        self._loss_alert_day = None
        self._alerts: dict[str, float] = {}

    async def _alert(self, text: str):
        """Error notification, same message at most every 30 minutes."""
        key = text[:60]
        if time.time() - self._alerts.get(key, 0) > 1800:
            self._alerts[key] = time.time()
            await self.notify(text)

    @property
    def paused(self) -> bool:
        return bool(self.db.get("paused", False))

    # ================================================================ entries
    async def try_open(self) -> str:
        if self.paused:
            return "Bot đang tạm dừng mở lệnh."
        open_trades = self.db.open_trades()
        if len(open_trades) >= C.MAX_OPEN_POSITIONS:
            return "Đã đủ số vị thế tối đa."
        bal = await self.acct.balances()
        if self.acct.realized_today() <= -C.DAILY_MAX_LOSS_PCT * bal["wallet"]:
            if self._loss_alert_day != today():
                self._loss_alert_day = today()
                await self.notify(f"⛔ Lỗ trong ngày chạm giới hạn {C.DAILY_MAX_LOSS_PCT:.0%}, ngừng mở lệnh mới hôm nay.")
            return "Chạm giới hạn lỗ ngày."

        positions = await self.client.positions()
        cooldown = time.time() * 1000 - C.SYMBOL_COOLDOWN_HOURS * 3600_000
        exclude = set(positions) | {t["symbol"] for t in open_trades}
        signals = [s for s in await self.scanner.scan(exclude) if self.db.last_sl_time(s.symbol) < cooldown]
        for sig in signals[:5]:
            try:
                async with self._lock:
                    ok, msg = await self.open_trade(sig, bal)
            except BinanceError as e:
                ok, msg = False, str(e)
                await self._alert(f"⚠️ Không mở được {sig.symbol}: {e.msg}")
            if ok:
                return msg
            log.info("skip %s %s: %s", sig.symbol, sig.side, msg)
        return "Không có tín hiệu đạt chuẩn."

    async def open_trade(self, sig: Signal, bal: dict) -> tuple[bool, str]:
        info = self.client.symbols[sig.symbol]
        capital = bal["wallet"]
        price = await self.client.mark_price(sig.symbol)
        plan, why = risk.plan_trade(sig.side, price, sig.atr, sig.swing, capital)
        if not plan:
            return False, why
        lev_max, mmr = await self.client.bracket_for(sig.symbol, plan.notional * (1 + C.DCA_MAX_RATIO))
        plan, why = risk.plan_trade(sig.side, price, sig.atr, sig.swing, capital, lev_max, mmr)
        if not plan:
            return False, why
        qty = info.round_qty(plan.qty)
        if not info.valid_qty(qty, price):
            return False, "khối lượng dưới mức tối thiểu của sàn"
        if qty * price / plan.leverage > bal["available"] * 0.9:
            return False, "không đủ ký quỹ khả dụng"

        await self.client.set_isolated(sig.symbol)
        await self.client.set_leverage(sig.symbol, plan.leverage)
        order = await self.client.market_order(sig.symbol, open_side(sig.side), qty)
        filled = float(order.get("executedQty") or 0)
        if filled <= 0:
            return False, f"lệnh không khớp: {order}"
        fill = float(order.get("avgPrice") or 0) or price

        s = risk.sign(sig.side)
        sl_dist = abs(plan.entry - plan.sl)
        sl = fill - s * sl_dist
        target = plan.target_usd * filled / plan.qty
        tp1, tp = risk.targets(sig.side, fill, filled, target)
        try:
            sl_ref = await self.client.place_close_trigger(sig.symbol, close_side(sig.side), "STOP_MARKET", sl)
        except BinanceError as e:
            await self.client.market_order(sig.symbol, close_side(sig.side), filled, reduce_only=True)
            await self.notify(f"⚠️ {sig.symbol}: không đặt được SL ({e.msg}), đã đóng vị thế ngay.")
            return False, "đặt SL thất bại"
        try:
            tp_ref = await self.client.place_close_trigger(sig.symbol, close_side(sig.side), "TAKE_PROFIT_MARKET", tp)
        except BinanceError as e:
            log.warning("TP order %s: %s (bot will monitor TP itself)", sig.symbol, e)
            tp_ref = None

        tid = self.db.create_trade(
            symbol=sig.symbol, side=sig.side, status="OPEN", opened_at=now_ms(), entry_price=fill, avg_price=fill,
            qty=filled, initial_qty=filled, leverage=plan.leverage, sl=sl_ref["price"], initial_sl=sl_ref["price"],
            tp=tp, tp1=tp1, atr=sig.atr, target_usd=target, risk_usd=plan.risk_usd * filled / plan.qty,
            capital_at_open=capital, score=sig.score, extreme=fill, sl_order=sl_ref, tp_order=tp_ref,
            reason=", ".join(sig.reasons))
        self.db.add_event(tid, "OPEN", fill, filled, f"lev {plan.leverage}x score {sig.score}")
        msg = (f"🟢 <b>MỞ {sig.side} {sig.symbol}</b> #{tid}\n"
               f"Giá: {info.fmt_price(fill)} | KL: {info.fmt_qty(filled)} | Đòn bẩy {plan.leverage}x (isolated)\n"
               f"Notional: {filled * fill:,.0f} USDT | Ký quỹ ~{filled * fill / plan.leverage:,.0f}\n"
               f"TP1: {info.fmt_price(tp1)} (chốt {C.TP1_CLOSE_RATIO:.0%}) | TP: {info.fmt_price(tp)}\n"
               f"SL: {info.fmt_price(sl)} | R:R {plan.rr:.2f}\n"
               f"Kỳ vọng: +{target:,.2f} ({target / capital:.2%} vốn) | Rủi ro: -{plan.risk_usd * filled / plan.qty:,.2f}\n"
               f"Điểm: {sig.score} — {', '.join(sig.reasons)}")
        await self.notify(msg)
        return True, msg

    # ================================================================ management
    async def manage(self, slow: bool = False):
        trades = self.db.open_trades()
        if not trades:
            return
        positions = await self.client.positions()
        for t in trades:
            p = positions.get(t["symbol"])
            try:
                async with self._lock:
                    if p is None or risk.sign(t["side"]) * float(p["positionAmt"]) <= 0:
                        await self.finalize(t)
                    else:
                        await self.manage_trade(t, p, slow)
            except Exception as e:
                log.exception("manage %s", t["symbol"])
                await self._alert(f"⚠️ Lỗi quản lý {t['symbol']}: {e}")

    async def manage_trade(self, t: dict, p: dict, slow: bool):
        side = t["side"]
        s = risk.sign(side)
        qty, avg, mark = abs(float(p["positionAmt"])), float(p["entryPrice"]), float(p["markPrice"])
        extreme = max(t["extreme"] or avg, mark) if s > 0 else min(t["extreme"] or avg, mark)
        upd = {"extreme": extreme}
        if abs(qty - t["qty"]) > 1e-12 or abs(avg - t["avg_price"]) > 1e-12:
            upd.update(qty=qty, avg_price=avg)
        self.db.update_trade(t["id"], **upd)
        t.update(upd)

        # exchange TP missing and price got there -> close ourselves
        if not t["tp_order"] and risk.crossed(side, mark, t["tp"]):
            await self.close_trade(t, "TP")
            return

        if not t["tp1_done"] and risk.crossed(side, mark, t["tp1"]):
            await self.take_partial(t, qty, avg, mark)
        elif t["tp1_done"]:
            new_sl = risk.trailing_stop(side, extreme, t["atr"], t["sl"], avg)
            if new_sl and s * (mark - new_sl) > 0.1 * t["atr"]:
                if await self.move_sl(t, new_sl):
                    self.db.add_event(t["id"], "TRAIL", new_sl, qty)

        if slow:
            await self.ensure_protection(t)
            await self.slow_checks(t, p, qty, avg, mark)

    async def take_partial(self, t: dict, qty: float, avg: float, mark: float):
        side, sym = t["side"], t["symbol"]
        info = self.client.symbols[sym]
        part = info.round_qty(qty * C.TP1_CLOSE_RATIO)
        done_txt = ""
        if info.valid_qty(part, mark) and info.valid_qty(qty - part, mark):
            o = await self.client.market_order(sym, close_side(side), part, reduce_only=True)
            px = float(o.get("avgPrice") or mark)
            gain = risk.sign(side) * (px - avg) * part
            done_txt = f"Chốt {info.fmt_qty(part)} @ {info.fmt_price(px)} (~{usd(gain)} USDT)\n"
            self.db.add_event(t["id"], "TP1", px, part, f"{gain:.2f}")
        be = risk.breakeven_price(side, avg)
        trail = risk.trailing_stop(side, t["extreme"], t["atr"], t["sl"], avg)
        new_sl = trail if trail and risk.sign(side) * (trail - be) > 0 else be
        moved = await self.move_sl(t, new_sl)
        self.db.update_trade(t["id"], tp1_done=1)
        t["tp1_done"] = 1
        await self.notify(f"🎯 <b>TP1 {sym}</b> #{t['id']}\n{done_txt}"
                          f"SL {'dời về' if moved else 'CHƯA dời được về'} hòa vốn+: {info.fmt_price(new_sl)} "
                          f"(giá TB {info.fmt_price(avg)}) — phần còn lại không thể lỗ.")

    async def move_sl(self, t: dict, new_sl: float) -> bool:
        sym, side = t["symbol"], t["side"]
        old = t["sl_order"]
        try:
            ref = await self.client.place_close_trigger(sym, close_side(side), "STOP_MARKET", new_sl)
            await self.client.cancel_trigger(sym, old)
        except BinanceError as e:
            log.info("place-then-cancel SL %s failed (%s), trying cancel-then-place", sym, e)
            await self.client.cancel_trigger(sym, old)
            try:
                ref = await self.client.place_close_trigger(sym, close_side(side), "STOP_MARKET", new_sl)
            except BinanceError as e2:
                ref = await self.client.place_close_trigger(sym, close_side(side), "STOP_MARKET", t["sl"])
                self.db.update_trade(t["id"], sl_order=ref)
                t["sl_order"] = ref
                log.warning("move SL %s -> %s failed: %s", sym, new_sl, e2)
                return False
        self.db.update_trade(t["id"], sl=ref["price"], sl_order=ref)
        t.update(sl=ref["price"], sl_order=ref)
        return True

    async def ensure_protection(self, t: dict):
        """Re-create SL/TP orders if they disappeared (manual cancel, expiry...)."""
        sym, side = t["symbol"], t["side"]
        live = await self.client.open_trigger_ids(sym)
        for key, otype, level in (("sl_order", "STOP_MARKET", t["sl"]), ("tp_order", "TAKE_PROFIT_MARKET", t["tp"])):
            ref = t[key]
            if ref and (ref["kind"], int(ref["id"])) in live:
                continue
            try:
                new = await self.client.place_close_trigger(sym, close_side(side), otype, level)
                self.db.update_trade(t["id"], **{key: new})
                t[key] = new
                if key == "sl_order":
                    await self.notify(f"🛡 {sym}: đặt lại lệnh SL @ {level}")
            except BinanceError as e:
                if key == "sl_order":
                    await self.notify(f"⚠️ {sym}: KHÔNG đặt lại được SL ({e.msg}). Kiểm tra ngay!")

    async def net_pnl(self, t: dict, p: dict) -> float:
        """Net PnL of the trade if closed now: realized + unrealized - fees + funding - closing fee."""
        fills = await self.client.user_trades(t["symbol"], t["opened_at"] - 1000)
        realized = sum(float(f["realizedPnl"]) for f in fills)
        fees = sum(float(f["commission"]) for f in fills if f["commissionAsset"] in C.STABLE_ASSETS)
        funding = sum(float(r["income"]) for r in await self.client.income(
            t["opened_at"] - 1000, symbol=t["symbol"], income_type="FUNDING_FEE"))
        qty, mark = abs(float(p["positionAmt"])), float(p["markPrice"])
        return realized - fees + funding + float(p["unRealizedProfit"]) - qty * mark * C.TAKER_FEE

    async def slow_checks(self, t: dict, p: dict, qty: float, avg: float, mark: float):
        side, sym = t["side"], t["symbol"]
        c1 = await self.scanner.candles(sym, "1h")
        net = await self.net_pnl(t, p)
        floor = t["capital_at_open"] * C.MIN_EXIT_PROFIT_PCT

        # 1) time exit: only in profit, required profit decays with age
        age_h = (now_ms() - t["opened_at"]) / 3600_000
        need = risk.required_exit_profit(age_h, t["target_usd"], t["capital_at_open"])
        if need is not None and net >= need:
            await self.close_trade(t, f"TIME_EXIT {age_h:.0f}h")
            return
        # 2) trend flipped against us: take the profit if there is one, otherwise keep SL in charge
        if trend_reversed(side, c1) and net >= floor:
            await self.close_trade(t, "TREND_REVERSAL")
            return
        # 3) DCA: price moved toward SL but higher-timeframe trend intact and a bounce confirmed
        if t["dca_count"] < C.DCA_MAX and not t["tp1_done"]:
            frac = risk.adverse_fraction(side, t["entry_price"], t["initial_sl"], mark)
            if C.DCA_TRIGGER <= frac <= C.DCA_MAX_ZONE:
                c4, c15 = await self.scanner.candles(sym, "4h"), await self.scanner.candles(sym, "15m")
                if trend_intact(side, c1, c4) and bounce_confirmed(side, c15):
                    await self.add_to_position(t, qty, avg, mark)

    async def add_to_position(self, t: dict, qty: float, avg: float, mark: float):
        side, sym = t["side"], t["symbol"]
        info = self.client.symbols[sym]
        add = info.round_qty(risk.dca_qty(side, avg, qty, t["sl"], mark, t["capital_at_open"], t["initial_qty"]))
        if not info.valid_qty(add, mark):
            return
        bal = await self.acct.balances()
        if add * mark / t["leverage"] > bal["available"] * 0.9:
            return
        o = await self.client.market_order(sym, open_side(side), add)
        px = float(o.get("avgPrice") or mark)
        p = await self.client.position(sym)
        new_qty = abs(float(p["positionAmt"])) if p else qty + add
        new_avg = float(p["entryPrice"]) if p else risk.new_average(avg, qty, px, add)
        tp1, tp = risk.targets(side, new_avg, new_qty, t["target_usd"])
        await self.client.cancel_trigger(sym, t["tp_order"])
        try:
            tp_ref = await self.client.place_close_trigger(sym, close_side(side), "TAKE_PROFIT_MARKET", tp)
        except BinanceError:
            tp_ref = None
        self.db.update_trade(t["id"], qty=new_qty, avg_price=new_avg, tp=tp, tp1=tp1, tp_order=tp_ref,
                             dca_count=t["dca_count"] + 1)
        self.db.add_event(t["id"], "DCA", px, add, f"avg {new_avg}")
        loss_at_sl = new_qty * abs(new_avg - t["sl"])
        await self.notify(
            f"➕ <b>DCA {sym}</b> #{t['id']}: thêm {info.fmt_qty(add)} @ {info.fmt_price(px)}\n"
            f"Giá TB: {info.fmt_price(avg)} → <b>{info.fmt_price(new_avg)}</b> | KL {info.fmt_qty(new_qty)}\n"
            f"TP1 mới: {info.fmt_price(tp1)} | TP mới: {info.fmt_price(tp)} | SL giữ nguyên: {info.fmt_price(t['sl'])}\n"
            f"Lỗ tối đa nếu chạm SL: -{loss_at_sl:,.2f} USDT")

    # ================================================================ exits
    async def close_trade(self, t: dict, reason: str):
        p = await self.client.position(t["symbol"])
        self.db.update_trade(t["id"], close_reason=reason)
        t["close_reason"] = reason
        if p:
            await self.client.market_order(t["symbol"], close_side(t["side"]), abs(float(p["positionAmt"])),
                                           reduce_only=True)
        await self.finalize(t)

    async def finalize(self, t: dict):
        """Position is flat: compute the real PnL from fills and close the record."""
        sym = t["symbol"]
        await self.client.cancel_all(sym)
        await asyncio.sleep(1.5)
        fills = await self.client.user_trades(sym, t["opened_at"] - 1000)
        realized = sum(float(f["realizedPnl"]) for f in fills)
        fees = sum(float(f["commission"]) for f in fills if f["commissionAsset"] in C.STABLE_ASSETS)
        funding = sum(float(r["income"]) for r in await self.client.income(
            t["opened_at"] - 1000, symbol=sym, income_type="FUNDING_FEE"))
        exits = [f for f in fills if f["side"] == close_side(t["side"])]
        eq = sum(float(f["qty"]) for f in exits[-20:])
        exit_px = sum(float(f["price"]) * float(f["qty"]) for f in exits[-20:]) / eq if eq else 0
        pnl = realized - fees + funding

        reason = t.get("close_reason")
        if not reason:
            last = float(exits[-1]["price"]) if exits else exit_px
            if abs(last - t["sl"]) <= abs(last - t["tp"]):
                reason = "SL" if pnl < 0 else ("TRAILING_SL" if t["tp1_done"] else "SL")
            else:
                reason = "TP"
        self.db.update_trade(t["id"], status="CLOSED", closed_at=now_ms(), exit_price=exit_px, pnl=pnl,
                             fees=fees, funding=funding, close_reason=reason)
        self.db.add_event(t["id"], "CLOSE", exit_px, 0, reason)
        info = self.client.symbols.get(sym)
        dur_h = (now_ms() - t["opened_at"]) / 3600_000
        icon = "✅" if pnl > 0 else "🔴"
        await self.notify(
            f"{icon} <b>ĐÓNG {t['side']} {sym}</b> #{t['id']} — {reason}\n"
            f"Vào TB: {info.fmt_price(t['avg_price']) if info else t['avg_price']} | "
            f"Ra: {info.fmt_price(exit_px) if info and exit_px else exit_px}\n"
            f"PnL ròng: <b>{usd(pnl)} USDT</b> ({pnl / t['capital_at_open']:+.2%} vốn) | "
            f"phí {fees:.2f} | funding {usd(funding)}\nThời gian giữ: {dur_h:.1f}h")

    async def manual_close(self, symbol: str, force: bool) -> str:
        t = next((x for x in self.db.open_trades() if x["symbol"] == symbol), None)
        if not t:
            return f"Không có vị thế bot đang mở cho {symbol}."
        async with self._lock:
            p = await self.client.position(symbol)
            if not p:
                await self.finalize(t)
                return "Vị thế đã đóng trên sàn, đã cập nhật."
            net = await self.net_pnl(t, p)
            if net < 0 and not force:
                return (f"{symbol} đang lỗ {usd(net)} USDT. Bot giữ nguyên chờ SL/hồi phục.\n"
                        f"Nếu vẫn muốn đóng: /close {symbol} force")
            await self.close_trade(t, "MANUAL")
        return f"Đã đóng {symbol}."

    # ================================================================ startup
    async def reconcile(self):
        positions = await self.client.positions()
        for t in self.db.open_trades():
            p = positions.get(t["symbol"])
            async with self._lock:
                if p is None:
                    await self.finalize(t)
                else:
                    await self.ensure_protection(t)
