"""Wires everything together: loops + Telegram commands."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import config as C
from .accounting import TZ, Accounting, fmt_ts, today, usd
from .binance_client import BinanceFutures
from .db import DB, now_ms
from .strategy import Scanner
from .telegram import Telegram
from .trader import Trader

log = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Tổng quan vốn, lãi/lỗ, vị thế"),
    ("positions", "Vị thế đang mở"),
    ("history", "Lịch sử lệnh đã đóng: /history 20"),
    ("trade", "Chi tiết 1 lệnh: /trade 12"),
    ("report", "Báo cáo ngày: /report hoặc /report 2026-09-30"),
    ("days", "Tổng hợp nhiều ngày: /days 7"),
    ("capital", "Vốn ban đầu, nạp/rút, ROI"),
    ("scan", "Quét tín hiệu ngay (không vào lệnh)"),
    ("pause", "Tạm dừng mở lệnh mới"),
    ("resume", "Tiếp tục mở lệnh"),
    ("close", "Đóng vị thế: /close BTCUSDT [force]"),
    ("help", "Danh sách lệnh"),
]


class App:
    def __init__(self, secrets):
        self.client = BinanceFutures(secrets.api_key, secrets.api_secret,
                                     C.TESTNET_URL if secrets.testnet else C.MAINNET_URL)
        self.db = DB(C.DB_PATH)
        self.tg = Telegram(secrets.tg_token, secrets.tg_chat_id)
        self.acct = Accounting(self.client, self.db)
        self.scanner = Scanner(self.client)
        self.trader = Trader(self.client, self.db, self.acct, self.scanner, self.tg.send)
        self.testnet = secrets.testnet

    # ------------------------------------------------------------ loops
    async def run(self):
        await self.client.sync_time()
        await self.client.load_exchange_info()
        await self.client.ensure_one_way_mode()
        first, bal = await self.acct.init_capital()
        await self.tg.set_commands(COMMANDS)
        net = "TESTNET" if self.testnet else "MAINNET"
        if first:
            await self.tg.send(f"🚀 Bot khởi động lần đầu ({net}).\nVốn ban đầu ghi nhận từ ví Futures: "
                               f"<b>{bal['wallet']:,.2f} USDT</b>\nNạp/rút sau này sẽ được tự động theo dõi.")
        else:
            await self.tg.send(f"🔄 Bot khởi động lại ({net}). Ví: {bal['wallet']:,.2f} | Equity: {bal['equity']:,.2f}")
        await self.trader.reconcile()
        await asyncio.gather(
            self._loop("manage", self._manage_tick, C.MANAGE_INTERVAL),
            self._loop("accounting", self._accounting_tick, C.ACCOUNTING_INTERVAL),
            self._scan_loop(),
            self.tg.poll(self.handle),
        )

    async def _loop(self, name, fn, interval):
        while True:
            try:
                await fn()
            except Exception as e:
                log.exception("%s loop", name)
                await self._alert(f"⚠️ Lỗi {name}: {e}")
            await asyncio.sleep(interval)

    _last_alert: dict = {}

    async def _alert(self, text: str):
        key = text[:60]
        if time.time() - self._last_alert.get(key, 0) > 1800:  # same error at most every 30 min
            self._last_alert[key] = time.time()
            await self.tg.send(text)

    _last_slow = 0.0

    async def _manage_tick(self):
        slow = time.time() - self._last_slow >= C.SLOW_CHECK_INTERVAL
        if slow:
            self._last_slow = time.time()
        await self.trader.manage(slow)

    async def _scan_loop(self):
        while True:
            now = time.time()
            nxt = (now // 900 + 1) * 900 + 20   # 20s after each 15m candle close
            await asyncio.sleep(nxt - now)
            try:
                if now // 3600 != nxt // 3600:  # hourly refresh of symbol filters
                    await self.client.load_exchange_info()
                msg = await self.trader.try_open()
                log.info("scan: %s", msg.splitlines()[0])
            except Exception as e:
                log.exception("scan")
                await self._alert(f"⚠️ Lỗi quét tín hiệu: {e}")

    async def _sync_income(self):
        for r in await self.acct.sync_income():
            amt = float(r["income"])
            await self.tg.send(f"{'💵 Nạp' if amt > 0 else '🏧 Rút'} {abs(amt):,.2f} {r['asset']} "
                               f"({fmt_ts(int(r['time']))}) — đã ghi nhận vào vốn đầu tư.")

    async def _accounting_tick(self):
        await self._sync_income()
        d = today()
        if self.db.snapshot(d) is None:  # first tick of a new day
            b = await self.acct.balances()
            self.db.save_snapshot(d, b["wallet"], b["equity"])
            y = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
            if self.db.snapshot(y) is not None:
                await self.tg.send(self.acct.daily_report(y) + "\n\n" +
                                   self.acct.capital_report(await self.acct.capital_summary()))

    # ------------------------------------------------------------ commands
    async def handle(self, cmd: str, args: list[str]) -> str:
        if cmd in ("start", "help"):
            return "🤖 <b>Lệnh hỗ trợ</b>\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)
        if cmd in ("status", "report", "days", "capital"):
            await self._sync_income()
        if cmd == "status":
            return await self.status()
        if cmd == "positions":
            return await self.positions_text()
        if cmd == "history":
            return self.history(int(args[0]) if args else 10)
        if cmd == "trade" and args:
            return self.trade_detail(int(args[0]))
        if cmd == "report":
            d = args[0] if args else today()
            eq = (await self.acct.balances())["equity"] if d == today() else None
            return self.acct.daily_report(d, eq)
        if cmd == "days":
            return self.acct.range_report(min(int(args[0]) if args else 7, 60))
        if cmd == "capital":
            return self.acct.capital_report(await self.acct.capital_summary())
        if cmd == "pause":
            self.db.set("paused", True)
            return "⏸ Đã tạm dừng mở lệnh mới (vị thế đang mở vẫn được quản lý)."
        if cmd == "resume":
            self.db.set("paused", False)
            return "▶️ Tiếp tục mở lệnh."
        if cmd == "close" and args:
            return await self.trader.manual_close(args[0].upper(), len(args) > 1 and args[1].lower() == "force")
        if cmd == "scan":
            sigs = await self.scanner.scan(set())
            if not sigs:
                return "Không có tín hiệu đạt chuẩn lúc này."
            return "🔎 <b>Tín hiệu</b>\n" + "\n".join(
                f"{s.symbol} {s.side} điểm {s.score} — {', '.join(s.reasons)}" for s in sigs[:10])
        return "Lệnh không hợp lệ. Gõ /help"

    async def status(self) -> str:
        cap = await self.acct.capital_summary()
        trades = self.db.open_trades()
        d = self.acct.day_stats(today())
        return (f"📌 <b>Trạng thái</b> {'⏸ TẠM DỪNG' if self.trader.paused else '▶️ đang chạy'}\n"
                f"Equity: <b>{cap['equity']:,.2f}</b> | Ví: {cap['wallet']:,.2f} | Khả dụng: {cap['available']:,.2f}\n"
                f"Vốn đầu tư ròng: {cap['invested']:,.2f} | Lãi/lỗ tổng: <b>{usd(cap['pnl'])} ({cap['roi']:+.2%})</b>\n"
                f"Hôm nay: {usd(d['net'])} USDT ròng, {len(d['closed'])} lệnh đóng\n"
                f"Vị thế bot: {len(trades)}/{C.MAX_OPEN_POSITIONS} | uPnL: {usd(cap['upnl'])}")

    async def positions_text(self) -> str:
        positions = await self.client.positions()
        trades = {t["symbol"]: t for t in self.db.open_trades()}
        if not positions:
            return "Không có vị thế đang mở."
        lines = ["📂 <b>Vị thế</b>"]
        for sym, p in positions.items():
            info = self.client.symbols.get(sym)
            fp = info.fmt_price if info else str
            amt, upnl = float(p["positionAmt"]), float(p["unRealizedProfit"])
            t = trades.get(sym)
            if not t:
                lines.append(f"\n{sym} {'LONG' if amt > 0 else 'SHORT'} {abs(amt)} (ngoài bot) uPnL {usd(upnl)}")
                continue
            age = (now_ms() - t["opened_at"]) / 3600_000
            lines.append(
                f"\n<b>#{t['id']} {sym} {t['side']}</b> {t['leverage']}x | giữ {age:.1f}h\n"
                f"KL {abs(amt)} | TB {fp(float(p['entryPrice']))} | Mark {fp(float(p['markPrice']))}\n"
                f"uPnL <b>{usd(upnl)}</b> | Thanh lý {fp(float(p['liquidationPrice'] or 0))}\n"
                f"SL {fp(t['sl'])} | TP1 {fp(t['tp1'])}{' ✅' if t['tp1_done'] else ''} | TP {fp(t['tp'])}"
                f" | DCA {t['dca_count']}/{C.DCA_MAX}")
        return "\n".join(lines)

    def history(self, n: int) -> str:
        rows = self.db.closed_trades(min(n, 50))
        if not rows:
            return "Chưa có lệnh nào đóng."
        total = sum(r["pnl"] or 0 for r in rows)
        wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
        lines = [f"📜 <b>{len(rows)} lệnh gần nhất</b> — thắng {wins}/{len(rows)}, tổng {usd(total)} USDT"]
        for r in rows:
            dur = ((r["closed_at"] or 0) - r["opened_at"]) / 3600_000
            lines.append(f"#{r['id']} {fmt_ts(r['closed_at'])} {r['symbol']} {r['side']} "
                         f"<b>{usd(r['pnl'] or 0)}</b> ({r['close_reason']}, {dur:.1f}h)")
        return "\n".join(lines)

    def trade_detail(self, tid: int) -> str:
        t = self.db.trade(tid)
        if not t:
            return "Không tìm thấy lệnh."
        info = self.client.symbols.get(t["symbol"])
        fp = (lambda x: info.fmt_price(x) if x else "-") if info else (lambda x: f"{x:g}" if x else "-")
        lines = [f"🧾 <b>#{t['id']} {t['symbol']} {t['side']}</b> — {t['status']}",
                 f"Mở {fmt_ts(t['opened_at'])} | Đóng {fmt_ts(t['closed_at'])}",
                 f"Vào {fp(t['entry_price'])} → TB {fp(t['avg_price'])} | Đòn bẩy {t['leverage']}x",
                 f"SL ban đầu {fp(t['initial_sl'])} | SL {fp(t['sl'])} | TP {fp(t['tp'])} | Điểm {t['score']}",
                 f"Lý do: {t['reason']}"]
        if t["status"] == "CLOSED":
            lines.append(f"Ra {fp(t['exit_price'])} | PnL {usd(t['pnl'] or 0)} (phí {t['fees'] or 0:.2f}, "
                         f"funding {usd(t['funding'] or 0)}) — {t['close_reason']}")
        lines.append("Diễn biến:")
        for e in self.db.events(tid):
            lines.append(f" • {fmt_ts(e['time'])} {e['type']} {fp(e['price'])} x {e['qty']:g} {e['info'] or ''}")
        return "\n".join(lines)

    async def close(self):
        await self.client.close()
        await self.tg.close()
