"""Thin async client for Binance USDⓈ-M Futures REST API."""
import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

CONDITIONAL_TYPES = ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET")


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"[{status}] {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


@dataclass
class SymbolInfo:
    symbol: str
    base: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: float
    onboard_ms: int

    def round_price(self, price: float) -> float:
        return float((Decimal(str(price)) / self.tick).quantize(Decimal(1), ROUND_HALF_UP) * self.tick)

    def round_qty(self, qty: float) -> float:
        q = (Decimal(str(qty)) / self.step).quantize(Decimal(1), ROUND_DOWN) * self.step
        return float(min(q, self.max_qty))

    def fmt_price(self, price: float) -> str:
        return format(Decimal(str(self.round_price(price))).quantize(self.tick.normalize()), "f")

    def fmt_qty(self, qty: float) -> str:
        return format(Decimal(str(self.round_qty(qty))).quantize(self.step.normalize()), "f")

    def valid_qty(self, qty: float, price: float) -> bool:
        return qty >= float(self.min_qty) and qty * price >= self.min_notional


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.key, self.secret = api_key, api_secret.encode()
        self.http = httpx.AsyncClient(base_url=base_url, timeout=15, headers={"X-MBX-APIKEY": api_key})
        self.time_offset = 0
        self.symbols: dict[str, SymbolInfo] = {}
        self._brackets: dict[str, list[dict]] = {}

    async def close(self):
        await self.http.aclose()

    # ------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(3):
            q = dict(params)
            if signed:
                q["timestamp"] = int(time.time() * 1000) + self.time_offset
                q["recvWindow"] = 10000
                qs = urlencode(q)
                qs += "&signature=" + hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
            else:
                qs = urlencode(q)
            try:
                r = await self.http.request(method, f"{path}?{qs}" if qs else path)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise
                log.warning("network error %s %s: %s", method, path, e)
                await asyncio.sleep(1 + attempt * 2)
                continue
            if r.status_code in (418, 429) or r.status_code >= 500:
                if attempt == 2:
                    break
                await asyncio.sleep(2 + attempt * 3)
                continue
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"msg": r.text[:200]}
            if r.status_code >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
                code = data.get("code") if isinstance(data, dict) else None
                if code == -1021 and attempt < 2:  # timestamp outside recvWindow
                    await self.sync_time()
                    continue
                raise BinanceError(r.status_code, code, data.get("msg", r.text) if isinstance(data, dict) else r.text)
            return data
        raise BinanceError(r.status_code, None, r.text)

    async def sync_time(self):
        data = await self._request("GET", "/fapi/v1/time")
        self.time_offset = int(data["serverTime"]) - int(time.time() * 1000)

    # ------------------------------------------------------------ market data
    async def load_exchange_info(self):
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        out = {}
        for s in data["symbols"]:
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            step = max(Decimal(lot["stepSize"]), Decimal(f["LOT_SIZE"]["stepSize"]))
            out[s["symbol"]] = SymbolInfo(
                symbol=s["symbol"], base=s["baseAsset"],
                tick=Decimal(f["PRICE_FILTER"]["tickSize"]), step=step,
                min_qty=max(Decimal(lot["minQty"]), Decimal(f["LOT_SIZE"]["minQty"])),
                max_qty=Decimal(lot["maxQty"]),
                min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                onboard_ms=int(s.get("onboardDate", 0)),
            )
        self.symbols = out

    async def tickers_24h(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> list[list]:
        return await self._request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def premium_index(self, symbol: str) -> dict:
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def mark_price(self, symbol: str) -> float:
        return float((await self.premium_index(symbol))["markPrice"])

    # ------------------------------------------------------------ account
    async def account(self) -> dict:
        return await self._request("GET", "/fapi/v2/account", signed=True)

    async def positions(self) -> dict[str, dict]:
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return {p["symbol"]: p for p in data if float(p["positionAmt"]) != 0}

    async def position(self, symbol: str) -> dict | None:
        data = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for p in data:
            if float(p["positionAmt"]) != 0:
                return p
        return None

    async def income(self, start_ms: int, end_ms: int | None = None, symbol: str | None = None,
                     income_type: str | None = None) -> list[dict]:
        """All income records since start_ms (paginates, 7-day windows)."""
        week = 7 * 86400 * 1000
        end_ms = end_ms or int(time.time() * 1000) + self.time_offset
        out, start = [], start_ms
        while start < end_ms:
            win_end = min(start + week - 1, end_ms)
            batch = await self._request("GET", "/fapi/v1/income", {
                "startTime": start, "endTime": win_end, "symbol": symbol,
                "incomeType": income_type, "limit": 1000}, signed=True)
            out.extend(batch)
            start = int(batch[-1]["time"]) + 1 if len(batch) == 1000 else win_end + 1
        return out

    async def user_trades(self, symbol: str, start_ms: int) -> list[dict]:
        week = 7 * 86400 * 1000
        end_ms = int(time.time() * 1000) + self.time_offset
        out, start = [], start_ms
        while start < end_ms:
            win_end = min(start + week - 1, end_ms)
            batch = await self._request("GET", "/fapi/v1/userTrades", {
                "symbol": symbol, "startTime": start, "endTime": win_end, "limit": 1000}, signed=True)
            out.extend(batch)
            start = int(batch[-1]["time"]) + 1 if len(batch) == 1000 else win_end + 1
        return out

    async def leverage_brackets(self, symbol: str) -> list[dict]:
        if symbol not in self._brackets:
            data = await self._request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True)
            item = data[0] if isinstance(data, list) else data
            self._brackets[symbol] = item["brackets"]
        return self._brackets[symbol]

    async def bracket_for(self, symbol: str, notional: float) -> tuple[int, float]:
        """(max leverage, maintenance margin ratio) for a notional size."""
        for b in await self.leverage_brackets(symbol):
            if notional <= float(b["notionalCap"]):
                return int(b["initialLeverage"]), float(b["maintMarginRatio"])
        return 1, 0.5

    async def ensure_one_way_mode(self):
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if data.get("dualSidePosition"):
            await self._request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, signed=True)

    async def set_leverage(self, symbol: str, leverage: int):
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_isolated(self, symbol: str):
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type"
                raise

    # ------------------------------------------------------------ orders
    async def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> dict:
        info = self.symbols[symbol]
        return await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": info.fmt_qty(qty),
            "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT"}, signed=True)

    async def place_close_trigger(self, symbol: str, side: str, order_type: str, trigger: float) -> dict:
        """Exchange-side SL / TP that closes the whole position.

        Binance moved conditional orders to the Algo Order API (Dec 2025); fall back to
        the classic endpoint for environments that still use it (e.g. older testnets).
        Returns {"kind": "algo"|"std", "id": ...}.
        """
        info = self.symbols[symbol]
        price = info.fmt_price(trigger)
        try:
            r = await self._request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
                "triggerPrice": price, "closePosition": "true", "workingType": "MARK_PRICE",
                "priceProtect": "TRUE"}, signed=True)
            return {"kind": "algo", "id": r["algoId"], "price": float(price)}
        except BinanceError as e:
            if e.status == 404 or e.code in (-1000, -1102, -1104, -5000):
                log.info("algo endpoint unavailable (%s), using /fapi/v1/order", e)
            else:
                raise
        r = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": order_type, "stopPrice": price,
            "closePosition": "true", "workingType": "MARK_PRICE", "priceProtect": "TRUE"}, signed=True)
        return {"kind": "std", "id": r["orderId"], "price": float(price)}

    async def cancel_trigger(self, symbol: str, ref: dict | None):
        if not ref:
            return
        try:
            if ref["kind"] == "algo":
                await self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": ref["id"]}, signed=True)
            else:
                await self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": ref["id"]}, signed=True)
        except BinanceError as e:
            log.info("cancel %s %s: %s", symbol, ref, e)  # already triggered / cancelled

    async def open_trigger_ids(self, symbol: str) -> set:
        ids = set()
        try:
            for o in await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True) or []:
                ids.add(("algo", int(o["algoId"])))
        except BinanceError as e:
            log.debug("openAlgoOrders: %s", e)
        for o in await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True):
            ids.add(("std", int(o["orderId"])))
        return ids

    async def cancel_all(self, symbol: str):
        for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
            try:
                await self._request("DELETE", path, {"symbol": symbol}, signed=True)
            except BinanceError as e:
                log.debug("cancel_all %s %s: %s", path, symbol, e)
