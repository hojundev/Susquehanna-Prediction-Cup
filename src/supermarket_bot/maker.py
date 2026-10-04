"""Market making: rest a bid below and an ask above fair value.

For each selected exchange the maker posts two resting limit orders around
the fair value ``p`` (from Kalshi / Polymarket):

* a YES bid at about ``p - half_spread``;
* a YES ask at about ``p + half_spread``, placed as a NO buy at ``1 - ask``.

If both fill, the YES and NO shares cancel into 1 SUSQie per pair, so the
round trip earns ``ask - bid`` whichever way the race goes. If only one side
fills, the maker holds that inventory. Inventory is kept small:

* **Skew** — both quotes shift away from the held side, so the side that
  reduces inventory becomes more attractive and the side that adds to it
  less so.
* **Cap** — once net position cost reaches ``maker_max_inventory``, the side
  that would add to it stops quoting.
* **Shared limits** — new inventory also respects the cash reserve, the
  per-race cap and the D-vs-R cap, plus the maker's own ``maker_budget``.

Pricing rules, all in YES terms:

* never at or through fair value (bid ≤ fair - tick, ask ≥ fair + tick);
* no better than one tick inside the current best, so the maker never pays
  more than needed to lead the book;
* never crossing the book, so quotes rest instead of taking liquidity.

The maker only manages exchanges recorded in its state file, plus new ones
it picks. It stays out of races where buy-and-hold holds a position, and
buy-and-hold stays out of the maker's exchanges, so the two strategies'
inventories never mix.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .fair_value import FairValue
from .models import MAX_PRICE, MIN_PRICE, TICK, Action, OrderIntent, Side, snap_price
from .risk import ExposureState, GroupLookup, LeanLookup, RiskManager, SizedTrade

log = logging.getLogger(__name__)

_EPS = 1e-9


@dataclass(frozen=True)
class Top:
    """Best bid and ask for one exchange, YES terms (None = empty side)."""

    bid: float | None
    ask: float | None


@dataclass(frozen=True)
class MakerInput:
    exchange_id: str
    market_id: str
    fair: FairValue
    top: Top
    #: Signed shares held: positive = YES, negative = NO.
    held: int


@dataclass(frozen=True)
class MakerQuote:
    exchange_id: str
    market_id: str
    bid: float | None  # YES price we buy at
    ask: float | None  # YES price we sell at (a NO buy at 1 - ask)


# --------------------------------------------------------------------------
# State: which exchanges the maker manages
# --------------------------------------------------------------------------
def load_owned(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        log.error("Could not read maker state %s (starting empty): %s", p, exc)
        return set()
    ids = raw.get("exchanges") if isinstance(raw, dict) else None
    return {str(x) for x in ids} if isinstance(ids, list) else set()


def save_owned(path: str | Path, owned: set[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"exchanges": sorted(owned)}, indent=2) + "\n")
    tmp.replace(p)


# --------------------------------------------------------------------------
# Pricing and sizing
# --------------------------------------------------------------------------
def _shares(budget: float, price: float) -> int:
    if budget <= 0 or price <= 0:
        return 0
    return int(budget / price + _EPS)


def quote_prices(
    fair: float, top: Top, half_spread: float, skew: float, inv_frac: float
) -> tuple[float | None, float | None]:
    """YES bid and ask for one exchange, on tick, or None for a side to skip.

    ``inv_frac`` is net inventory cost / cap in [-1, 1] (positive = long YES).
    """
    center = fair - skew * inv_frac
    bid = min(center - half_spread, fair - TICK)
    ask = max(center + half_spread, fair + TICK)
    if top.bid is not None:
        bid = min(bid, top.bid + TICK)  # lead the best bid by one tick at most
        ask = max(ask, top.bid + TICK)  # an ask at/below the best bid would take liquidity
    if top.ask is not None:
        ask = max(ask, top.ask - TICK)  # lead the best ask by one tick at most
        bid = min(bid, top.ask - TICK)  # a bid at/above the best ask would take liquidity
    out_bid = snap_price(bid, up=False) if MIN_PRICE - _EPS <= bid <= MAX_PRICE else None
    out_ask = snap_price(ask, up=True) if MIN_PRICE <= ask <= MAX_PRICE + _EPS else None
    return out_bid, out_ask


class MarketMaker:
    def __init__(
        self,
        settings: Settings,
        risk: RiskManager,
        lean_of: LeanLookup,
        group_of: GroupLookup,
    ) -> None:
        self._s = settings
        self._risk = risk
        self._lean_of = lean_of
        self._group = group_of

    def _eligible(self, inp: MakerInput) -> bool:
        s = self._s
        p = inp.fair.probability
        return s.maker_min_fair <= p <= s.maker_max_fair and inp.fair.confidence >= (
            s.maker_min_confidence
        )

    def _rank(self, inp: MakerInput, owned: set[str]) -> tuple[int, int, int, float]:
        """Sort key: manage inventory first, then owned, then best opportunity."""
        s = self._s
        bid, ask = quote_prices(inp.fair.probability, inp.top, s.maker_half_spread, s.maker_skew, 0)
        top = inp.top
        # How many of our two quotes would be at or ahead of the current best.
        leads = int(bid is not None and (top.bid is None or bid > top.bid - _EPS)) + int(
            ask is not None and (top.ask is None or ask < top.ask + _EPS)
        )
        width = (top.ask - top.bid) if top.ask is not None and top.bid is not None else 1.0
        return (
            0 if inp.held else 1,
            0 if inp.exchange_id in owned else 1,
            -leads,
            -width,
        )

    def plan(
        self, inputs: list[MakerInput], state: ExposureState, owned: set[str]
    ) -> tuple[list[SizedTrade], set[str]]:
        """Quotes for this cycle, reserving their capital in ``state``.

        Returns the trades and the set of exchanges quoted.
        """
        s = self._s
        picked = sorted(
            (i for i in inputs if self._eligible(i)), key=lambda i: self._rank(i, owned)
        )
        trades: list[SizedTrade] = []
        quoted: set[str] = set()
        for inp in picked[: s.maker_max_markets]:
            trade = self._quote(inp, state)
            if trade is not None:
                trades.append(trade)
                quoted.add(inp.exchange_id)
        return trades, quoted

    def _budget(self, inp: MakerInput, side: Side, state: ExposureState) -> float:
        maker_left = self._s.maker_budget * state.equity - state.maker_cost
        return min(
            maker_left,
            self._risk.spendable(state),
            self._risk.race_headroom(inp.market_id, state),
            self._risk.correlation_headroom(inp.exchange_id, side, state),
        )

    def _side(
        self, inp: MakerInput, side: Side, price: float, state: ExposureState, reason: str
    ) -> OrderIntent | None:
        """Size one side. Shares that net against held inventory cost no cash."""
        s = self._s
        signed_cost = state.per_exchange_cost.get(inp.exchange_id, 0.0)
        same_cost = max(0.0, side.sign * signed_cost)
        reducing = max(0, -side.sign * inp.held)  # opposite-side shares this order nets
        base = _shares(s.maker_quote_size, price)
        free = min(base, reducing)
        room = min(
            _shares(s.maker_max_inventory - same_cost, price),
            _shares(self._budget(inp, side, state), price),
        )
        adding = max(0, min(base - free, room))
        qty = free + adding
        if qty < s.maker_min_shares:
            return None
        if adding:
            state.reserve_maker(
                self._group(inp.market_id), inp.exchange_id, side, adding * price, self._lean_of
            )
        return OrderIntent(
            exchange_id=inp.exchange_id,
            side=side,
            action=Action.BUY,
            quantity=qty,
            price=price,
            reason=reason,
        )

    def _quote(self, inp: MakerInput, state: ExposureState) -> SizedTrade | None:
        s = self._s
        fair = inp.fair.probability
        signed_cost = state.per_exchange_cost.get(inp.exchange_id, 0.0)
        inv_frac = max(-1.0, min(1.0, signed_cost / s.maker_max_inventory))
        bid, ask = quote_prices(fair, inp.top, s.maker_half_spread, s.maker_skew, inv_frac)
        top = inp.top
        why = (
            f"maker fair={fair:.3f} quote {_fmt(bid)}/{_fmt(ask)} "
            f"book {_fmt(top.bid)}/{_fmt(top.ask)} inv={signed_cost:+.0f}"
        )

        intents: list[OrderIntent] = []
        if bid is not None:
            o = self._side(inp, Side.YES, bid, state, why)
            if o is not None:
                intents.append(o)
        if ask is not None:
            no_price = round(1.0 - ask, 3)
            if MIN_PRICE - _EPS <= no_price <= MAX_PRICE + _EPS:
                o = self._side(inp, Side.NO, no_price, state, why)
                if o is not None:
                    intents.append(o)
        if not intents:
            return None
        log.debug("MAKER %s %s", inp.exchange_id, why)
        return SizedTrade("maker", intents)


def _fmt(x: float | None) -> str:
    return "-" if x is None or math.isnan(x) else f"{x:.3f}"
