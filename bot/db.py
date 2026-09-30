"""SQLite storage: capital baseline, income ledger, trades, daily snapshots."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS income (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tran_id TEXT, time INTEGER, symbol TEXT, income_type TEXT, income REAL, asset TEXT, info TEXT,
    UNIQUE (tran_id, income_type, symbol, asset)
);
CREATE INDEX IF NOT EXISTS idx_income_time ON income(time);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, side TEXT, status TEXT,
    opened_at INTEGER, closed_at INTEGER,
    entry_price REAL, avg_price REAL, qty REAL, initial_qty REAL, leverage INTEGER,
    sl REAL, initial_sl REAL, tp REAL, tp1 REAL, tp1_done INTEGER DEFAULT 0, dca_count INTEGER DEFAULT 0,
    atr REAL, target_usd REAL, risk_usd REAL, capital_at_open REAL, score REAL, extreme REAL,
    sl_order TEXT, tp_order TEXT,
    exit_price REAL, pnl REAL, fees REAL, funding REAL, close_reason TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, time INTEGER, type TEXT, price REAL, qty REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (date TEXT PRIMARY KEY, time INTEGER, wallet REAL, equity REAL);
"""

JSON_COLS = ("sl_order", "tp_order")


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ meta
    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    # ------------------------------------------------------------ income
    def add_income(self, rows: list[dict]) -> list[dict]:
        """Insert income rows from Binance; returns only rows that were new."""
        new = []
        for r in rows:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO income(tran_id, time, symbol, income_type, income, asset, info) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(r.get("tranId")), int(r["time"]), r.get("symbol") or "", r["incomeType"],
                 float(r["income"]), r.get("asset", ""), r.get("info", "")))
            if cur.rowcount:
                new.append(r)
        self.conn.commit()
        return new

    def income_sum(self, start_ms: int, end_ms: int, types: tuple[str, ...], assets: tuple[str, ...]) -> float:
        q = (f"SELECT COALESCE(SUM(income),0) FROM income WHERE time>=? AND time<? "
             f"AND income_type IN ({','.join('?' * len(types))}) AND asset IN ({','.join('?' * len(assets))})")
        return self.conn.execute(q, (start_ms, end_ms, *types, *assets)).fetchone()[0]

    def transfers(self, start_ms: int = 0, end_ms: int | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM income WHERE income_type='TRANSFER' AND time>=? AND time<? ORDER BY time",
            (start_ms, end_ms or 2**62)).fetchall()

    # ------------------------------------------------------------ trades
    def _row(self, row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for c in JSON_COLS:
            d[c] = json.loads(d[c]) if d[c] else None
        return d

    def create_trade(self, **fields) -> int:
        for c in JSON_COLS:
            if c in fields:
                fields[c] = json.dumps(fields[c]) if fields[c] else None
        cols = ",".join(fields)
        cur = self.conn.execute(f"INSERT INTO trades({cols}) VALUES ({','.join('?' * len(fields))})",
                                tuple(fields.values()))
        self.conn.commit()
        return cur.lastrowid

    def update_trade(self, trade_id: int, **fields):
        for c in JSON_COLS:
            if c in fields:
                fields[c] = json.dumps(fields[c]) if fields[c] else None
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))
        self.conn.commit()

    def trade(self, trade_id: int) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())

    def open_trades(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id")]

    def closed_trades(self, limit: int = 10, start_ms: int = 0, end_ms: int | None = None) -> list[dict]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM trades WHERE status='CLOSED' AND closed_at>=? AND closed_at<? "
            "ORDER BY closed_at DESC LIMIT ?", (start_ms, end_ms or 2**62, limit))]

    def trades_opened_between(self, start_ms: int, end_ms: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM trades WHERE opened_at>=? AND opened_at<?",
                                 (start_ms, end_ms)).fetchone()[0]

    def last_sl_time(self, symbol: str) -> int:
        row = self.conn.execute("SELECT MAX(closed_at) FROM trades WHERE symbol=? AND close_reason='SL' AND pnl<0",
                                (symbol,)).fetchone()
        return row[0] or 0

    def add_event(self, trade_id: int, type_: str, price: float = 0, qty: float = 0, info: str = ""):
        self.conn.execute("INSERT INTO trade_events(trade_id, time, type, price, qty, info) VALUES (?,?,?,?,?,?)",
                          (trade_id, now_ms(), type_, price, qty, info))
        self.conn.commit()

    def events(self, trade_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trade_events WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()

    # ------------------------------------------------------------ snapshots
    def save_snapshot(self, date: str, wallet: float, equity: float):
        self.conn.execute("INSERT OR IGNORE INTO snapshots(date, time, wallet, equity) VALUES (?,?,?,?)",
                          (date, now_ms(), wallet, equity))
        self.conn.commit()

    def snapshot(self, date: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM snapshots WHERE date=?", (date,)).fetchone()
