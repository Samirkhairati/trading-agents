"""Persistent simulated paper account (the default broker). Orders fill instantly at the latest yfinance price.
State (cash, holdings, deposits, ledger) lives in the state dir, so it survives restarts and redeployments."""
import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Tuple

from trading import state as st
from trading.broker import FakeBroker
from trading.types import ProposedOrder

START_CASH = float(os.environ.get("SIM_START_CASH", "100000"))
_lock = threading.RLock()
_cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (fetched at, price)

def _fetch(symbol: str) -> float:
    import yfinance as yf
    return float(yf.Ticker(symbol).history(period="5d")["Close"].iloc[-1])

class SimBroker(FakeBroker):
    def __init__(self):
        super().__init__(cash=START_CASH)
        self.deposited = START_CASH
        self.ledger: List[dict] = []
        self._load()

    def _load(self) -> None:
        d = st._read("sim_account.json", None)
        if not d:
            return
        self.cash, self.deposited, self.ledger = d["cash"], d["deposited"], d.get("ledger", [])
        self.holdings = {s: (h["qty"], h["avg_entry"]) for s, h in d["holdings"].items()}
        self.prices.update(d.get("prices", {}))

    def _save(self) -> None:
        st._write("sim_account.json", {
            "cash": self.cash, "deposited": self.deposited, "ledger": self.ledger[-500:], "prices": self.prices,
            "holdings": {s: {"qty": q, "avg_entry": a} for s, (q, a) in self.holdings.items()}})

    def _log(self, kind: str, **kw) -> None:
        self.ledger.append({"time": datetime.now().astimezone().isoformat(timespec="seconds"), "type": kind, **kw})

    def get_price(self, symbol: str) -> float:
        hit = _cache.get(symbol)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        try:
            price = _fetch(symbol)
            _cache[symbol] = (time.monotonic(), price)
            self.prices[symbol] = price
            return price
        except Exception:
            if symbol in self.prices:  # last known price beats failing the whole dashboard
                return self.prices[symbol]
            raise ValueError(f"No price for {symbol}")

    def place_order(self, order: ProposedOrder) -> str:
        if order.side == "sell":
            return self.close_position(order.symbol)
        with _lock:
            self._load()
            oid = super().place_order(order)
            self._log("buy", symbol=order.symbol, notional=round(order.notional, 2), price=self.prices.get(order.symbol), reason=order.reason[:200])
            self._save()
            return oid

    def close_position(self, symbol: str, source: str = "order") -> str:
        with _lock:
            self._load()
            qty, avg = self.holdings.get(symbol, (0.0, 0.0))
            price = self.get_price(symbol) if symbol in self.holdings else 0.0
            oid = super().close_position(symbol)
            self._log("sell", symbol=symbol, qty=qty, price=price, pnl=round((price - avg) * qty, 2), source=source)
            self._save()
            return oid

    def deposit(self, amount: float) -> None:
        with _lock:
            self._load()
            self.cash += amount
            self.deposited += amount
            self._log("deposit", amount=round(amount, 2))
            self._save()
