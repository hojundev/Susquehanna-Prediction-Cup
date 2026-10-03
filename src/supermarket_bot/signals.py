"""Signal engine: market data + fair values + positions -> trade candidates.

All books are YES-perspective. A YES bid at ``b`` is also a NO ask at
``1 - b``, and a YES ask at ``a`` is a NO bid at ``1 - a``. Every leg price
below is *side-relative* (what you pay or receive per share of that side).

Signal kinds, in execution priority:

1. ``close`` — take profit: you hold a side with a fair-value view and the
   market has reached that value, so sell into the book.
2. ``arbitrage`` — multi-outcome (mutually exclusive) markets only:

   * **NO basket**: buy NO on every outcome. At most one outcome wins, so at
     least ``n - 1`` NO shares pay out. Cost ``n - sum(bids)``, so profit is at
     least ``sum(bids) - 1``. Safe whether or not the outcomes are exhaustive.
   * **YES basket**: buy YES on every outcome for ``sum(asks) < 1``. Only safe
     when exactly one outcome must win, so it is opt-in.

   A binary market has a single contract, so it has no internal arbitrage
   (YES ask + NO ask = 1 + spread, which is always >= 1).
3. ``open`` — mispricing: fair value differs from the executable price by at
   least ``min_edge``. The book is walked level by level while the edge holds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .fair_value import FairValue, FairValueModel
from .models import Action, Market, MarketOrderbook, OrderbookExchange, OrderbookLevel, Side

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Leg:
    exchange_id: str
    side: Side
    action: Action
    price: float  # side-relative limit price
    available: int  # shares available at `price` or better


@dataclass
class TradeCandidate:
    kind: str  # "close" | "arbitrage" | "open"
    market_id: str
    legs: list[Leg]
    #: open: expected profit per share at the limit price.
    #: arbitrage: locked profit per basket. close: 0.
    edge: float
    reason: str
    confidence: float = 1.0
    fair: FairValue | None = None
    #: close only: shares currently held (caps the sell quantity).
    held: int = 0

    @property
    def basket_cost(self) -> float:
        return sum(leg.price for leg in self.legs if leg.action is Action.BUY)


def _walk(levels: list[OrderbookLevel], keep: Callable[[float], bool]) -> tuple[float | None, int]:
    """Walk book levels while ``keep(yes_price)`` holds.

    Returns ``(worst_yes_price_taken, total_quantity)``.
    """
    worst: float | None = None
    total = 0.0
    for level in levels:
        if level.quantity <= 0 or not keep(level.price):
            break
        worst = level.price
        total += level.quantity
    return worst, int(total)


class SignalEngine:
    def __init__(self, settings: Settings, fair_value: FairValueModel) -> None:
        self._s = settings
        self._fv = fair_value

    # -- arbitrage -------------------------------------------------------
    def find_arbitrage(self, market: Market, book: MarketOrderbook) -> list[TradeCandidate]:
        # Composite markets are not mutually exclusive outcomes; >10 legs cannot
        # be placed atomically.
        if market.is_composite or not 2 <= len(book.exchanges) <= 10:
            return []
        out: list[TradeCandidate] = []

        bids = [ex.best_bid() for ex in book.exchanges]
        if all(b is not None for b in bids):
            levels = [b for b in bids if b is not None]
            profit = sum(b.price for b in levels) - 1.0
            if profit >= self._s.min_arb_profit:
                legs = [
                    Leg(ex.exchange_id, Side.NO, Action.BUY, 1.0 - b.price, int(b.quantity))
                    for ex, b in zip(book.exchanges, levels, strict=True)
                ]
                out.append(
                    TradeCandidate(
                        kind="arbitrage",
                        market_id=market.id,
                        legs=legs,
                        edge=profit,
                        reason=f"NO basket: sum(bids)={profit + 1:.3f} over {len(legs)} outcomes",
                    )
                )

        if self._s.allow_yes_basket_arb:
            asks = [ex.best_ask() for ex in book.exchanges]
            if all(a is not None for a in asks):
                levels = [a for a in asks if a is not None]
                profit = 1.0 - sum(a.price for a in levels)
                if profit >= self._s.min_arb_profit:
                    legs = [
                        Leg(ex.exchange_id, Side.YES, Action.BUY, a.price, int(a.quantity))
                        for ex, a in zip(book.exchanges, levels, strict=True)
                    ]
                    out.append(
                        TradeCandidate(
                            kind="arbitrage",
                            market_id=market.id,
                            legs=legs,
                            edge=profit,
                            reason=f"YES basket: sum(asks)={1 - profit:.3f}",
                        )
                    )

        for cand in out:
            log.info("ARB %s %s", market.id, cand.reason)
        # Only one basket per market: they compete for the same capital.
        return sorted(out, key=lambda c: -c.edge)[:1]

    # -- take profit -----------------------------------------------------
    def _close_for(
        self, market_id: str, ex: OrderbookExchange, fv: FairValue, held_signed: int
    ) -> TradeCandidate | None:
        if not self._s.take_profit or held_signed == 0:
            return None
        if held_signed > 0:
            # Hold YES: sell YES into bids at or above fair value.
            worst, qty = _walk(ex.bids, lambda p: p >= fv.probability)
            if worst is None:
                return None
            leg = Leg(ex.exchange_id, Side.YES, Action.SELL, worst, qty)
            why = f"bid>={worst:.3f} reached fair={fv.probability:.3f}"
        else:
            # Hold NO: selling NO at 1-a == lifting YES asks at or below fair.
            worst, qty = _walk(ex.asks, lambda p: p <= fv.probability)
            if worst is None:
                return None
            leg = Leg(ex.exchange_id, Side.NO, Action.SELL, 1.0 - worst, qty)
            why = f"ask<={worst:.3f} reached fair={fv.probability:.3f}"
        return TradeCandidate(
            kind="close",
            market_id=market_id,
            legs=[leg],
            edge=0.0,
            reason=f"take profit: {why}",
            fair=fv,
            held=abs(held_signed),
        )

    # -- mispricing ------------------------------------------------------
    def _open_for(
        self, market_id: str, ex: OrderbookExchange, fv: FairValue
    ) -> TradeCandidate | None:
        q, m = fv.probability, self._s.min_edge

        # A huge gap is far more likely a bad mapping or stale view than real edge.
        ask, bid = ex.best_ask(), ex.best_bid()
        gap = max(q - ask.price if ask else 0.0, bid.price - q if bid else 0.0)
        if gap > self._s.max_edge:
            log.warning(
                "Skip %s: fair %.3f vs book %s/%s differs by %.3f > max_edge %.2f "
                "(check the mapping or your view)",
                ex.exchange_id,
                q,
                bid.price if bid else "-",
                ask.price if ask else "-",
                gap,
                self._s.max_edge,
            )
            return None

        worst, qty = _walk(ex.asks, lambda p: q - p >= m)
        if worst is not None:
            return TradeCandidate(
                kind="open",
                market_id=market_id,
                legs=[Leg(ex.exchange_id, Side.YES, Action.BUY, worst, qty)],
                edge=q - worst,
                reason=f"fair={q:.3f} vs ask<={worst:.3f}",
                confidence=fv.confidence,
                fair=fv,
            )

        worst, qty = _walk(ex.bids, lambda p: p - q >= m)
        if worst is not None:
            return TradeCandidate(
                kind="open",
                market_id=market_id,
                legs=[Leg(ex.exchange_id, Side.NO, Action.BUY, 1.0 - worst, qty)],
                edge=worst - q,
                reason=f"fair={q:.3f} vs bid>={worst:.3f} (buy NO)",
                confidence=fv.confidence,
                fair=fv,
            )
        return None

    # -- public ----------------------------------------------------------
    def evaluate(
        self, market: Market, book: MarketOrderbook, holdings: dict[str, int]
    ) -> list[TradeCandidate]:
        """Candidates for one market.

        ``holdings`` maps exchange id to signed share count (+YES / -NO).
        An exchange with a close signal gets no open signal in the same cycle.
        """
        out = self.find_arbitrage(market, book)
        for ex in book.exchanges:
            fv = self._fv.estimate(ex.exchange_id)
            if fv is None:
                continue
            close = self._close_for(market.id, ex, fv, holdings.get(ex.exchange_id, 0))
            if close is not None:
                out.append(close)
                continue
            opened = self._open_for(market.id, ex, fv)
            if opened is not None:
                out.append(opened)
        return out
