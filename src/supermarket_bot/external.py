"""Read-only price feeds from Kalshi and Polymarket.

Both sites publish market data without an account or API key. The bot only
reads; it never places orders on either site.

* Kalshi: ``GET /markets?tickers=A,B`` returns YES bid/ask in dollars.
* Polymarket (Gamma API): ``GET /markets?id=X&id=Y`` returns ``bestBid`` /
  ``bestAsk`` for each binary market's YES outcome.

Each cycle makes one batched request per source (chunked), far under either
site's published rate limits.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

KALSHI_URL = "https://external-api.kalshi.com/trade-api/v2"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
_UA = {"User-Agent": "supermarket-bot/0.3 (read-only)", "Accept": "application/json"}
_CHUNK = 50


@dataclass(frozen=True)
class Quote:
    """YES bid/ask for one external contract, as probabilities (0-1)."""

    source: str
    ref: str
    bid: float
    ask: float
    volume_24h: float = 0.0
    title: str = ""

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def inverted(self) -> Quote:
        """The complement contract's quote (YES <-> NO)."""
        return Quote(
            self.source, self.ref, 1.0 - self.ask, 1.0 - self.bid, self.volume_24h, self.title
        )


@dataclass(frozen=True)
class Candidate:
    """An external contract that might match a Super Market exchange."""

    source: str
    ref: str
    text: str  # what the matcher compares against
    title: str  # human-readable label
    bid: float | None = None
    ask: float | None = None


def _quote(source: str, ref: str, bid: Any, ask: Any, volume: Any, title: str) -> Quote | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    # A missing side shows up as 0 bid or 0/1 ask; a crossed book is unusable.
    if not (0.0 < b < 1.0 and 0.0 < a < 1.0 and b <= a):
        return None
    try:
        v = float(volume or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return Quote(source, ref, b, a, v, title)


def _chunks(items: list[str], n: int = _CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class _Transient(Exception):
    pass


class _Http:
    def __init__(
        self, base_url: str, timeout: float, transport: httpx.BaseTransport | None
    ) -> None:
        self._c = httpx.Client(base_url=base_url, timeout=timeout, headers=_UA, transport=transport)

    @retry(
        retry=retry_if_exception_type((_Transient, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, max=8),
        reraise=True,
    )
    def get(self, path: str, params: Any = None) -> Any:
        r = self._c.get(path, params=params)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Transient(f"{r.status_code} on {path}")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._c.close()


class KalshiFeed:
    source = "kalshi"

    def __init__(
        self,
        base_url: str = KALSHI_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = _Http(base_url, timeout, transport)

    def close(self) -> None:
        self._http.close()

    def quotes(self, tickers: Iterable[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for chunk in _chunks(sorted(set(tickers))):
            payload = self._http.get("/markets", {"tickers": ",".join(chunk)})
            for m in payload.get("markets", []):
                if m.get("status") not in ("active", "open"):
                    continue
                q = _quote(
                    self.source,
                    m["ticker"],
                    m.get("yes_bid_dollars"),
                    m.get("yes_ask_dollars"),
                    m.get("volume_24h_fp"),
                    m.get("title", ""),
                )
                if q:
                    out[q.ref] = q
        return out

    def election_candidates(self, max_pages: int = 40) -> list[Candidate]:
        """Every open Elections/Politics market (for match suggestions)."""
        out: list[Candidate] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            payload = self._http.get("/events", params)
            for ev in payload.get("events", []):
                if ev.get("category") not in ("Elections", "Politics"):
                    continue
                for m in ev.get("markets") or []:
                    if m.get("status") not in ("active", "open"):
                        continue
                    sub = m.get("yes_sub_title") or ""
                    title = f"{ev.get('title', '')} — {sub}".strip(" —")
                    q = _quote(
                        self.source,
                        m["ticker"],
                        m.get("yes_bid_dollars"),
                        m.get("yes_ask_dollars"),
                        None,
                        title,
                    )
                    out.append(
                        Candidate(
                            self.source,
                            m["ticker"],
                            f"{ev.get('title', '')} {m.get('title', '')} {sub}",
                            title,
                            q.bid if q else None,
                            q.ask if q else None,
                        )
                    )
            cursor = payload.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)  # stay well under the read budget
        return out


class PolymarketFeed:
    source = "polymarket"

    def __init__(
        self,
        base_url: str = POLYMARKET_GAMMA_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = _Http(base_url, timeout, transport)

    def close(self) -> None:
        self._http.close()

    def quotes(self, ids: Iterable[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for chunk in _chunks(sorted(set(ids))):
            payload = self._http.get("/markets", [("id", i) for i in chunk])
            for m in payload if isinstance(payload, list) else []:
                if m.get("closed") or not m.get("active"):
                    continue
                q = _quote(
                    self.source,
                    str(m["id"]),
                    m.get("bestBid"),
                    m.get("bestAsk"),
                    m.get("volume24hr"),
                    m.get("question", ""),
                )
                if q:
                    out[q.ref] = q
        return out

    def search(self, query: str, limit: int = 10) -> list[Candidate]:
        payload = self._http.get(
            "/public-search", {"q": query, "limit_per_type": limit, "events_status": "active"}
        )
        out: list[Candidate] = []
        for ev in payload.get("events") or []:
            if ev.get("closed"):
                continue
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("active"):
                    continue
                group = m.get("groupItemTitle") or ""
                question = m.get("question", "")
                q = _quote(
                    self.source, str(m["id"]), m.get("bestBid"), m.get("bestAsk"), None, question
                )
                out.append(
                    Candidate(
                        self.source,
                        str(m["id"]),
                        f"{ev.get('title', '')} {question} {group}",
                        question,
                        q.bid if q else None,
                        q.ask if q else None,
                    )
                )
        return out
