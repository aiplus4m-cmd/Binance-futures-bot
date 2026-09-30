"""Signal scanner: multi-timeframe trend-pullback strategy with a quality score.

- 4h  : regime (EMA20 vs EMA50, price vs EMA50)
- 1h  : trend + strength (EMA20/50, ADX), ATR for SL/TP sizing, room to next swing
- 15m : entry trigger (pullback into EMA20 then momentum resumes: RSI crosses 50, candle closes back over EMA20)
Only the highest-scoring setup (>= MIN_SCORE) is traded.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from . import config as C
from .indicators import adx, atr, ema, rsi

log = logging.getLogger(__name__)


@dataclass
class Candles:
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]

    @classmethod
    def from_klines(cls, rows: list[list], drop_last: bool = True) -> "Candles":
        rows = rows[:-1] if drop_last else rows  # last kline is still forming
        return cls(*[[float(r[i]) for r in rows] for i in (1, 2, 3, 4, 5)])


@dataclass
class Signal:
    symbol: str
    side: str
    score: float
    price: float
    atr: float
    swing: float
    reasons: list[str] = field(default_factory=list)


def evaluate(symbol: str, c4: Candles, c1: Candles, c15: Candles, funding: float = 0.0) -> Signal | None:
    if min(len(c4.c), len(c1.c), len(c15.c)) < 60:
        return None
    e20_4, e50_4 = ema(c4.c, 20), ema(c4.c, 50)
    e20_1, e50_1 = ema(c1.c, 20), ema(c1.c, 50)
    adx1 = adx(c1.h, c1.l, c1.c)[-1]
    atr1 = atr(c1.h, c1.l, c1.c)[-1]
    rsi1 = rsi(c1.c)[-1]
    e20_15 = ema(c15.c, 20)
    rsi15 = rsi(c15.c)
    atr15 = atr(c15.h, c15.l, c15.c)[-1]
    price = c15.c[-1]
    if not (atr1 > 0 and adx1 == adx1):
        return None

    if e20_4[-1] > e50_4[-1] and c4.c[-1] > e50_4[-1] and e20_1[-1] > e50_1[-1] and c1.c[-1] > e50_1[-1]:
        side, s = "LONG", 1
    elif e20_4[-1] < e50_4[-1] and c4.c[-1] < e50_4[-1] and e20_1[-1] < e50_1[-1] and c1.c[-1] < e50_1[-1]:
        side, s = "SHORT", -1
    else:
        return None
    if adx1 < 20:
        return None
    if s * (c1.c[-1] - e20_1[-1]) / atr1 > 1.5:  # overextended from the 1h mean
        return None

    # 15m pullback into EMA20 within the last 8 candles, then momentum resumes
    n = 8
    touched = any(
        (c15.l[i] <= e20_15[i] + 0.1 * atr15) if s > 0 else (c15.h[i] >= e20_15[i] - 0.1 * atr15)
        for i in range(-n, -1))
    reclaimed = s * (price - e20_15[-1]) > 0 and s * (c15.c[-1] - c15.o[-1]) > 0
    r_now = rsi15[-1]
    r_prev_min = min(rsi15[-6:-1]) if s > 0 else max(rsi15[-6:-1])
    momentum = (50 < r_now < 70 and r_prev_min <= 50) if s > 0 else (30 < r_now < 50 and r_prev_min >= 50)
    if not (touched and reclaimed and momentum):
        return None

    swing = min(c15.l[-12:]) if s > 0 else max(c15.h[-12:])
    reasons = [f"4h+1h trend {side}", f"ADX {adx1:.0f}", f"RSI15 {r_now:.0f}"]
    score = 40.0
    score += min(20.0, max(0.0, adx1 - 20))
    gap4 = s * (e20_4[-1] - e50_4[-1]) / atr1
    score += min(10.0, gap4 * 3)

    vol_avg = sum(c15.v[-21:-1]) / 20
    vr = c15.v[-1] / vol_avg if vol_avg else 0
    if vr >= 1.5:
        score += 10
        reasons.append(f"vol x{vr:.1f}")
    elif vr >= 1.2:
        score += 5

    # room to the nearest 1h swing in trade direction vs a 2 ATR target
    ext = max(c1.h[-48:]) if s > 0 else min(c1.l[-48:])
    room = s * (ext - price)
    if room <= 0 or room >= 2 * atr1:
        score += 10
    elif room < atr1:
        score -= 15
        reasons.append("gần kháng cự/hỗ trợ")

    if s * funding < 0:
        score += 5
    elif s * funding > 0.0005:
        score -= 5
    if (s > 0 and 50 <= rsi1 <= 68) or (s < 0 and 32 <= rsi1 <= 50):
        score += 5
    elif (s > 0 and rsi1 > 75) or (s < 0 and rsi1 < 25):
        score -= 10

    return Signal(symbol, side, round(min(score, 100), 1), price, atr1, swing, reasons)


def trend_intact(side: str, c1: Candles, c4: Candles) -> bool:
    """Higher-timeframe trend still supports the position (used before a DCA)."""
    s = 1 if side == "LONG" else -1
    e50_4 = ema(c4.c, 50)[-1]
    e20_1, e50_1 = ema(c1.c, 20)[-1], ema(c1.c, 50)[-1]
    return s * (c4.c[-1] - e50_4) > 0 and s * (e20_1 - e50_1) > 0


def bounce_confirmed(side: str, c15: Candles) -> bool:
    """Last closed 15m candle turned in our favour and RSI isn't still collapsing."""
    s = 1 if side == "LONG" else -1
    r = rsi(c15.c)
    return s * (c15.c[-1] - c15.o[-1]) > 0 and s * (r[-1] - r[-2]) > 0


def trend_reversed(side: str, c1: Candles) -> bool:
    s = 1 if side == "LONG" else -1
    e20, e50 = ema(c1.c, 20)[-1], ema(c1.c, 50)[-1]
    return s * (e20 - e50) < 0 and s * (c1.c[-1] - e50) < 0


class Scanner:
    def __init__(self, client):
        self.client = client
        self._sem = asyncio.Semaphore(6)

    async def candles(self, symbol: str, interval: str, limit: int = 150) -> Candles:
        async with self._sem:
            return Candles.from_klines(await self.client.klines(symbol, interval, limit))

    async def universe(self) -> list[str]:
        tickers = await self.client.tickers_24h()
        min_onboard = (time.time() - C.MIN_LISTING_DAYS * 86400) * 1000
        rows = []
        for t in tickers:
            info = self.client.symbols.get(t["symbol"])
            if not info or info.base in C.EXCLUDED_BASES or info.onboard_ms > min_onboard:
                continue
            qv = float(t["quoteVolume"])
            if qv < C.MIN_QUOTE_VOLUME or abs(float(t["priceChangePercent"])) > C.MAX_24H_CHANGE_PCT:
                continue
            rows.append((qv, t["symbol"]))
        rows.sort(reverse=True)
        return [s for _, s in rows[:C.UNIVERSE_SIZE]]

    async def btc_bias(self) -> int:
        """-1 = BTC dumping (no longs), +1 = pumping (no shorts), 0 = neutral."""
        c1 = await self.candles("BTCUSDT", "1h", 5)
        chg = (c1.c[-1] - c1.c[-3]) / c1.c[-3]
        return -1 if chg < -0.015 else (1 if chg > 0.015 else 0)

    async def analyze(self, symbol: str) -> Signal | None:
        try:
            c4, c1, c15 = await asyncio.gather(self.candles(symbol, "4h"), self.candles(symbol, "1h"),
                                               self.candles(symbol, "15m"))
            funding = float((await self.client.premium_index(symbol)).get("lastFundingRate", 0))
            return evaluate(symbol, c4, c1, c15, funding)
        except Exception as e:  # one bad symbol must not stop the scan
            log.warning("analyze %s: %s", symbol, e)
            return None

    async def scan(self, exclude: set[str]) -> list[Signal]:
        symbols = [s for s in await self.universe() if s not in exclude]
        bias = await self.btc_bias()
        results = await asyncio.gather(*(self.analyze(s) for s in symbols))
        signals = [r for r in results if r and r.score >= C.MIN_SCORE
                   and not (bias < 0 and r.side == "LONG") and not (bias > 0 and r.side == "SHORT")]
        signals.sort(key=lambda x: x.score, reverse=True)
        return signals
