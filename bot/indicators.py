"""Minimal indicator set (pure Python, no numpy)."""


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _wilder(values: list[float], period: int) -> list[float]:
    out, avg = [], None
    for i, v in enumerate(values):
        if i < period - 1:
            out.append(float("nan"))
        elif i == period - 1:
            avg = sum(values[:period]) / period
            out.append(avg)
        else:
            avg = (avg * (period - 1) + v) / period
            out.append(avg)
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = _wilder(gains[1:], period), _wilder(losses[1:], period)
    out = [float("nan")]
    for g, l in zip(ag, al):
        if g != g:  # nan
            out.append(float("nan"))
        else:
            out.append(100.0 if l == 0 else 100 - 100 / (1 + g / l))
    return out


def true_ranges(highs, lows, closes) -> list[float]:
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs, lows, closes, period: int = 14) -> list[float]:
    return _wilder(true_ranges(highs, lows, closes), period)


def adx(highs, lows, closes, period: int = 14) -> list[float]:
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = _wilder(true_ranges(highs, lows, closes), period)
    pdm, mdm = _wilder(plus_dm, period), _wilder(minus_dm, period)
    dx = []
    for t, p, m in zip(tr, pdm, mdm):
        if t != t or t == 0:
            dx.append(float("nan"))
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    valid = [d for d in dx if d == d]
    lead = len(dx) - len(valid)
    return [float("nan")] * lead + _wilder(valid, period) if len(valid) >= period else [float("nan")] * len(dx)
