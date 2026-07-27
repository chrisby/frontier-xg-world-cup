"""Kalshi trade-api v2 client.

- Market data (markets/events/orderbook) is PUBLIC — works with no credentials.
- Trading (balance/orders) needs an API key (key id + RSA private key) and
  RSA-PSS request signing. Those methods are guarded so the read/analysis path
  never requires secrets.

Prices are in CENTS (1-99). cents/100 == implied probability.
"""
import base64
import time
from typing import Optional

import requests

from . import config


class KalshiReadClient:
    """Public market-data client. No auth required."""

    def __init__(self, base: str = None):
        self.base = base or config.KALSHI_BASE
        self.session = requests.Session()

    def _get(self, path: str, params: dict = None) -> dict:
        # retry with backoff on rate-limit (429) and transient server errors
        for attempt in range(5):
            r = self.session.get(f"{self.base}{path}", params=params or {},
                                 timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def markets(self, series_ticker: str = None, event_ticker: str = None,
                status: str = "open", limit: int = 200) -> list:
        out, cursor = [], None
        while True:
            p = {"status": status, "limit": min(limit, 1000)}
            if series_ticker:
                p["series_ticker"] = series_ticker
            if event_ticker:
                p["event_ticker"] = event_ticker
            if cursor:
                p["cursor"] = cursor
            d = self._get("/markets", p)
            out.extend(d.get("markets", []))
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                break
        return out

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._get(f"/markets/{ticker}/orderbook", {"depth": depth}).get(
            "orderbook", {}
        )

    def book_prices(self, ticker: str) -> dict:
        """Both sides of the order book, in cents.

        The list/market endpoints return null summary prices for these markets;
        the real liquidity is in the book. Buying YES crosses the best NO bid
        (yes_ask = 100 - best_no_bid); buying NO crosses the best YES bid
        (no_ask = 100 - best_yes_bid). Handles the full-precision `orderbook_fp`
        (dollar strings) and the legacy `orderbook` (cents).
        """
        try:
            d = self._get(f"/markets/{ticker}/orderbook")
        except Exception:
            return {}
        fp = d.get("orderbook_fp") or {}
        legacy = d.get("orderbook") or {}

        def best(levels) -> float | None:
            prices = []
            for lvl in levels or []:
                try:
                    p = float(lvl[0])
                    prices.append(p if p <= 1 else p / 100.0)
                except (ValueError, TypeError, IndexError):
                    continue
            return max(prices) if prices else None

        no_bid = best(fp.get("no_dollars") or legacy.get("no"))
        yes_bid = best(fp.get("yes_dollars") or legacy.get("yes"))
        yb = round(yes_bid * 100) if yes_bid is not None else 0
        nb = round(no_bid * 100) if no_bid is not None else 0
        # No real two-sided market -> the top-of-book is noise. A legit longshot
        # still passes (its strong opposite-side bid keeps the spread tight).
        if 100 - yb - nb > config.MAX_SPREAD_CENTS:
            return {}
        out = {}
        if yb:
            out["yes_bid"] = yb
            out["no_ask"] = 100 - yb
        if nb:
            out["no_bid"] = nb
            out["yes_ask"] = 100 - nb
        return {k: v for k, v in out.items() if 0 < v < 100}

    def yes_ask_cents(self, ticker: str) -> int | None:
        """Live YES ask (cents) from the order book; None if no liquidity."""
        return self.book_prices(ticker).get("yes_ask")

    @staticmethod
    def implied_prob(market: dict) -> Optional[float]:
        """Mid implied probability from yes bid/ask (cents). None if no book."""
        bid, ask = market.get("yes_bid"), market.get("yes_ask")
        if bid and ask:
            return (bid + ask) / 200.0
        last = market.get("last_price")
        return last / 100.0 if last else None


def live_bankroll() -> float | None:
    """Live Kalshi cash balance in USD, or None if credentials aren't available."""
    try:
        bal = KalshiTradeClient().balance()
    except Exception:
        return None
    if bal.get("balance_dollars") is not None:
        return float(bal["balance_dollars"])
    if bal.get("balance") is not None:
        return bal["balance"] / 100.0  # cents -> dollars
    return None


class KalshiTradeClient(KalshiReadClient):
    """Authenticated client for balance + order placement. Needs RSA key.

    Only instantiated at go-live (human-approved order step).
    """

    def __init__(self, key_id: str = None, private_key_path: str = None, base: str = None):
        super().__init__(base)
        from cryptography.hazmat.primitives import serialization  # lazy import

        self.key_id = key_id or config.KALSHI_KEY_ID
        path = private_key_path or config.KALSHI_PRIVATE_KEY_PATH
        if not self.key_id or not path:
            raise RuntimeError("KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set")
        with open(path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, ts_ms: str, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        msg = f"{ts_ms}{method}{path}".encode()
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def balance(self) -> dict:
        path = "/trade-api/v2/portfolio/balance"
        r = self.session.get(
            self.base.replace("/trade-api/v2", "") + path,
            headers=self._headers("GET", path), timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def settlements(self, limit: int = 200) -> list:
        """All settlements on this account (paginated, most recent first)."""
        path = "/trade-api/v2/portfolio/settlements"
        out, cursor = [], None
        while True:
            params = {"limit": min(limit, 1000)}
            if cursor:
                params["cursor"] = cursor
            r = self.session.get(
                self.base.replace("/trade-api/v2", "") + path,
                headers=self._headers("GET", path), params=params, timeout=30,
            )
            r.raise_for_status()
            d = r.json()
            out.extend(d.get("settlements", []))
            cursor = d.get("cursor")
            if not cursor or not d.get("settlements"):
                break
        return out

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    price_cents: int, order_type: str = "limit") -> dict:
        """Human-approved order placement. side=yes/no, action=buy/sell.

        Maps to Kalshi v2 orders API (POST /portfolio/events/orders):
          buy YES  → bid at yes_price dollars
          buy NO   → ask at (100 - no_price) dollars (sell YES = buy NO)
        """
        path = "/trade-api/v2/portfolio/events/orders"
        if action == "buy":
            if side == "yes":
                new_side = "bid"
                price_dollars = price_cents / 100.0
            else:  # no
                new_side = "ask"
                price_dollars = (100 - price_cents) / 100.0
        else:
            raise ValueError(f"Unsupported action: {action}")
        body = {
            "ticker": ticker,
            "side": new_side,
            "count": f"{count:.2f}",
            "price": f"{price_dollars:.4f}",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": f"wc-{int(time.time()*1000)}",
        }
        r = self.session.post(
            self.base.replace("/trade-api/v2", "") + path,
            headers=self._headers("POST", path), json=body, timeout=30,
        )
        r.raise_for_status()
        return r.json()
