"""Capital tracking (initial balance, deposits/withdrawals) and reports."""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as C
from .db import DB, now_ms

log = logging.getLogger(__name__)
TZ = ZoneInfo(C.TIMEZONE)
PNL_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR", "LIQUIDATION_FEE")


def day_bounds(date_str: str) -> tuple[int, int]:
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(d.timestamp() * 1000), int((d + timedelta(days=1)).timestamp() * 1000)


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def fmt_ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d/%m %H:%M") if ms else "-"


def usd(x: float) -> str:
    return f"{x:+,.2f}" if x else "0.00"


class Accounting:
    def __init__(self, client, db: DB):
        self.client, self.db = client, db

    async def balances(self) -> dict:
        a = await self.client.account()
        return {"wallet": float(a["totalWalletBalance"]), "equity": float(a["totalMarginBalance"]),
                "available": float(a["availableBalance"]), "upnl": float(a["totalUnrealizedProfit"])}

    async def init_capital(self) -> tuple[bool, dict]:
        """Record the futures wallet balance as initial capital on first start."""
        b = await self.balances()
        if self.db.get("initial_capital") is None:
            self.db.set("initial_capital", b["wallet"])
            self.db.set("start_ms", now_ms())
            self.db.set("income_cursor", now_ms())
            self.db.save_snapshot(today(), b["wallet"], b["equity"])
            return True, b
        return False, b

    async def sync_income(self) -> list[dict]:
        """Pull new income (transfers, pnl, fees, funding). Returns new TRANSFER rows."""
        cursor = self.db.get("income_cursor", now_ms())
        rows = await self.client.income(cursor - 60_000)  # small overlap, dedup by unique key
        new = self.db.add_income(rows)
        if rows:
            self.db.set("income_cursor", max(int(r["time"]) for r in rows))
        return [r for r in new if r["incomeType"] == "TRANSFER"]

    # ------------------------------------------------------------ capital
    def flows(self, start_ms: int = 0, end_ms: int | None = None) -> tuple[float, float]:
        dep = wd = 0.0
        for r in self.db.transfers(start_ms, end_ms):
            if r["asset"] not in C.STABLE_ASSETS:
                continue
            if r["income"] > 0:
                dep += r["income"]
            else:
                wd += -r["income"]
        return dep, wd

    async def capital_summary(self) -> dict:
        b = await self.balances()
        initial = self.db.get("initial_capital", b["wallet"])
        dep, wd = self.flows(self.db.get("start_ms", 0))
        invested = initial + dep - wd
        pnl = b["equity"] - invested
        return {**b, "initial": initial, "deposits": dep, "withdrawals": wd, "invested": invested,
                "pnl": pnl, "roi": pnl / invested if invested else 0.0, "start_ms": self.db.get("start_ms")}

    def realized_today(self) -> float:
        s, e = day_bounds(today())
        return self.db.income_sum(s, e, PNL_TYPES, C.STABLE_ASSETS)

    # ------------------------------------------------------------ reports
    def day_stats(self, date_str: str) -> dict:
        s, e = day_bounds(date_str)
        by_type = {t: self.db.income_sum(s, e, (t,), C.STABLE_ASSETS) for t in PNL_TYPES}
        net = sum(by_type.values())
        dep, wd = self.flows(s, e)
        closed = self.db.closed_trades(1000, s, e)
        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        snap = self.db.snapshot(date_str)
        next_snap = self.db.snapshot((datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
        return {"date": date_str, "realized": by_type["REALIZED_PNL"], "commission": by_type["COMMISSION"],
                "funding": by_type["FUNDING_FEE"], "net": net, "deposits": dep, "withdrawals": wd,
                "opened": self.db.trades_opened_between(s, e), "closed": closed, "wins": len(wins),
                "start_equity": snap["equity"] if snap else None,
                "end_equity": next_snap["equity"] if next_snap else None}

    def daily_report(self, date_str: str, current_equity: float | None = None) -> str:
        d = self.day_stats(date_str)
        end_eq = d["end_equity"] if d["end_equity"] is not None else current_equity
        lines = [f"📊 <b>Báo cáo ngày {date_str}</b>",
                 f"PnL đã chốt: <b>{usd(d['realized'])}</b> USDT",
                 f"Phí GD: {usd(d['commission'])} | Funding: {usd(d['funding'])}",
                 f"<b>Lãi/lỗ ròng: {usd(d['net'])} USDT</b>"]
        if d["start_equity"]:
            lines.append(f"Vốn đầu ngày: {d['start_equity']:,.2f} → {'cuối ngày' if d['end_equity'] is not None else 'hiện tại'}: "
                         f"{(end_eq or 0):,.2f}  ({d['net'] / d['start_equity']:+.2%} theo PnL ròng)")
        if d["deposits"] or d["withdrawals"]:
            lines.append(f"Nạp: +{d['deposits']:,.2f} | Rút: -{d['withdrawals']:,.2f}")
        n = len(d["closed"])
        lines.append(f"Lệnh mở: {d['opened']} | Lệnh đóng: {n}" + (f" | Thắng {d['wins']}/{n}" if n else ""))
        for t in d["closed"][:15]:
            lines.append(f" • {t['symbol']} {t['side']} {usd(t['pnl'] or 0)} ({t['close_reason']})")
        return "\n".join(lines)

    def range_report(self, days: int) -> str:
        lines = [f"📅 <b>Tổng hợp {days} ngày</b>", "<pre>Ngày        Ròng      Lệnh  Thắng"]
        total = 0.0
        base = datetime.now(TZ)
        for i in range(days - 1, -1, -1):
            ds = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            d = self.day_stats(ds)
            total += d["net"]
            lines.append(f"{ds}  {d['net']:>+9.2f}  {len(d['closed']):>4}  {d['wins']:>5}")
        lines.append(f"{'Tổng':<10}  {total:>+9.2f}</pre>")
        return "\n".join(lines)

    def capital_report(self, cap: dict) -> str:
        lines = [
            "💰 <b>Vốn</b>",
            f"Bắt đầu từ: {fmt_ts(cap['start_ms'])}",
            f"Vốn ban đầu: {cap['initial']:,.2f} USDT",
            f"Nạp thêm: +{cap['deposits']:,.2f} | Rút: -{cap['withdrawals']:,.2f}",
            f"Vốn đầu tư ròng: <b>{cap['invested']:,.2f}</b>",
            f"Ví: {cap['wallet']:,.2f} | Equity: <b>{cap['equity']:,.2f}</b> (uPnL {usd(cap['upnl'])})",
            f"Lãi/lỗ tổng: <b>{usd(cap['pnl'])} USDT ({cap['roi']:+.2%})</b>",
        ]
        rows = self.db.transfers(cap["start_ms"] or 0)[-10:]
        if rows:
            lines.append("\nNạp/rút gần đây:")
            for r in rows:
                lines.append(f" • {fmt_ts(r['time'])} {'Nạp' if r['income'] > 0 else 'Rút'} "
                             f"{abs(r['income']):,.2f} {r['asset']}")
        return "\n".join(lines)
