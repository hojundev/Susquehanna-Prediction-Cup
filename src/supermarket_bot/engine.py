"""Trading engine: one cycle wires data -> signals -> sizing -> execution;
the run loop repeats it until stopped.

Cycle order:

1. Read the tournament (window, cash). Outside the trading window a live
   cycle does nothing.
2. Live only: cancel our stale open orders so remainders never pile up and
   their cash is freed before sizing.
3. Read positions and rank; reload fair values.
4. Scan open markets, check linked-market relationships against those same
   books, then execute in priority order: closes (reduce risk), arbitrage
   (locked profit, within one market or across linked markets), then
   mispricing opens by descending edge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import (
    ApiError,
    AuthError,
    ResidenceUpdateRequiredError,
    SuperMarketClient,
    TermsNotAcknowledgedError,
)
from .config import Settings
from .execution import OrderExecutor
from .fair_value import FairValueModel
from .maker import MakerInput, MarketMaker, Top, load_owned, save_owned
from .models import (
    BulkExchangePrice,
    Market,
    MarketStatus,
    OrderbookExchange,
    Position,
    TournamentSummary,
    parse_ts,
)
from .relationships import Relationship, RelationshipArbitrage, parse_hints
from .risk import ExposureState, RiskManager, SizedTrade, race_key
from .signals import SignalEngine, TradeCandidate

log = logging.getLogger(__name__)

_PRIORITY = {"close": 0, "arbitrage": 1, "open": 2}
_FATAL = (TermsNotAcknowledgedError, ResidenceUpdateRequiredError, AuthError)


@dataclass
class CycleReport:
    traded: bool = False
    markets_scanned: int = 0
    candidates: int = 0
    orders_submitted: int = 0
    maker_exchanges: int = 0
    skipped_reason: str = ""


class TradingEngine:
    def __init__(
        self, settings: Settings, client: SuperMarketClient, fair_value: FairValueModel
    ) -> None:
        self._s = settings
        self._client = client
        self._fv = fair_value
        self._signals = SignalEngine(settings, fair_value)
        self._race: dict[str, str] = {}
        self._risk = RiskManager(settings, self._lean_of, self._group_of)
        self._executor = OrderExecutor(client, settings.dry_run, settings.order_ttl_seconds)
        self._rel_arb = RelationshipArbitrage(settings.min_arb_profit)
        self._relationships: list[Relationship] = []
        self._relationships_loaded = False
        self._maker = MarketMaker(settings, self._risk, self._lean_of, self._group_of)
        # Exchanges the maker manages. Ignored (treated as empty) when the
        # maker is off, so buy-and-hold takes over any leftover inventory.
        self._owned: set[str] = (
            load_owned(settings.maker_state_path) if settings.maker_enabled else set()
        )
        self._bulk: dict[str, BulkExchangePrice] = {}
        self._cycles = 0
        self._stop = False

    def _group_of(self, market_id: str) -> str:
        """Risk group for a market: its race, or the id itself if unknown."""
        return self._race.get(market_id) or market_id

    def _lean_of(self, exchange_id: str) -> str | None:
        fv = self._fv.estimate(exchange_id)
        return fv.lean if fv else None

    def stop(self) -> None:
        self._stop = True

    def _kelly_scale(self, rank: int | None) -> float:
        if self._s.defend_rank and rank is not None and rank <= self._s.defend_rank:
            log.info("Rank %d <= defend_rank=%d: scaling sizing down", rank, self._s.defend_rank)
            return self._s.defend_kelly_scale
        return 1.0

    # -- relationship arbitrage ------------------------------------------
    def _load_relationships(self) -> None:
        """Refresh the relationship graph every N cycles (also when it's empty)."""
        if not self._s.use_relationship_arb:
            return
        due = self._cycles % self._s.relationship_refresh_cycles == 0
        if self._relationships_loaded and not due:
            return
        try:
            raw = self._client.list_relationships()
        except _FATAL:
            raise
        except Exception as exc:  # noqa: BLE001 - relationships are optional
            log.warning("Relationship data unavailable this cycle: %s", exc)
            return
        self._relationships = [Relationship.from_json(r) for r in raw]
        self._relationships_loaded = True
        log.info("Loaded %d relationships", len(self._relationships))

    def _relationship_candidates(self, books: dict[str, OrderbookExchange]) -> list[TradeCandidate]:
        if not self._s.use_relationship_arb or not self._relationships:
            return []
        try:
            hints = {}
            if any(r.type == "monotonic" for r in self._relationships):
                hints = parse_hints(self._client.get_constraints(violations_only=True))
        except _FATAL:
            raise
        except Exception as exc:  # noqa: BLE001 - relationships are optional
            log.warning("Relationship data unavailable this cycle: %s", exc)
            return []
        return self._rel_arb.find(self._relationships, books, hints)

    # -- market pre-screen -----------------------------------------------
    def _markets_to_fetch(
        self, markets: list[Market], holdings: dict[str, int], allow_open: bool = True
    ) -> list[str]:
        """Market ids whose full orderbook is worth fetching this cycle.

        Full orderbooks are slow (often 10s+ each, rate limited), but every
        signal starts from the best bid/ask. So best bid/ask for all markets
        is read in bulk first, and only markets where a trade is possible
        get a full fetch. On any doubt the market is fetched.

        ``allow_open=False`` means buy-and-hold has no room for new positions,
        so only take-profit on held positions can trade; new-position and
        arbitrage edges are not worth a fetch.
        """
        all_ids = [m.id for m in markets]
        self._bulk = {}
        if not self._s.bulk_price_scan:
            return all_ids
        try:
            quotes, missing = self._client.bulk_prices(
                [ex.id for m in markets for ex in m.exchanges]
            )
        except _FATAL:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the full scan
            log.warning("Bulk prices unavailable (%s); scanning every orderbook", exc)
            return all_ids
        self._bulk = quotes  # the market maker reuses these best bid/ask

        linked = {mid for r in self._relationships for mid in r.market_of.values() if mid}
        keep: list[str] = []
        for m in markets:
            ids = [ex.id for ex in m.exchanges]
            if m.id in linked or any(e in missing or e not in quotes for e in ids):
                keep.append(m.id)
                continue
            qs = [quotes[e] for e in ids]
            if self._could_trade(m, qs, holdings, allow_open):
                keep.append(m.id)
        log.info(
            "Pre-screen: %d of %d markets could trade (bulk prices for %d exchanges)%s",
            len(keep),
            len(markets),
            len(quotes),
            "" if allow_open else "; buy-and-hold at cap, only take-profit checked",
        )
        return keep

    def _could_trade(
        self,
        market: Market,
        quotes: list[BulkExchangePrice],
        holdings: dict[str, int],
        allow_open: bool = True,
    ) -> bool:
        s = self._s
        for q in quotes:
            fv = self._fv.estimate(q.exchange_id)
            if fv is None:
                continue
            p = fv.probability
            held = holdings.get(q.exchange_id, 0)
            # Take profit: held side has reached fair value.
            if held > 0 and q.best_bid is not None and q.best_bid >= p:
                return True
            if held < 0 and q.best_ask is not None and q.best_ask <= p:
                return True
            if not allow_open:
                continue
            # Open: YES cheap, or YES rich (buy NO).
            if q.best_ask is not None and p - q.best_ask >= s.min_edge:
                return True
            if q.best_bid is not None and q.best_bid - p >= s.min_edge:
                return True
        if not allow_open:
            return False
        # Arbitrage across a multi-outcome market's outcomes.
        if not market.is_composite and 2 <= len(quotes) <= 10:
            bids = [q.best_bid for q in quotes if q.best_bid is not None]
            if len(bids) == len(quotes) and sum(bids) - 1.0 >= s.min_arb_profit:
                return True
            asks = [q.best_ask for q in quotes if q.best_ask is not None]
            if (
                s.allow_yes_basket_arb
                and len(asks) == len(quotes)
                and 1.0 - sum(asks) >= s.min_arb_profit
            ):
                return True
        return False

    # -- one cycle -------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        report = CycleReport()
        live = not self._s.dry_run

        tournament = self._client.resolve_tournament()
        is_open, why = tournament.trading_open(datetime.now(timezone.utc))
        if not is_open:
            if live:
                report.skipped_reason = why
                log.info("Not trading: %s", why)
                return report
            log.warning("Trading window closed (%s); continuing because dry_run=true", why)

        cash = tournament.my_balance
        if live and self._s.cancel_stale_orders:
            cancelled = self._client.cancel_all()
            if cancelled:
                log.info("Cancelled %d stale open orders", cancelled)
                cash = self._client.resolve_tournament().my_balance
        elif live and self._s.maker_enabled and self._owned:
            # Maker quotes are replaced every cycle, so last cycle's must go
            # even when general stale-order cancelling is off.
            cancelled = sum(self._client.cancel_all(eid) for eid in sorted(self._owned))
            if cancelled:
                log.info("Cancelled %d stale maker quotes", cancelled)
                cash = self._client.resolve_tournament().my_balance

        positions = self._client.get_positions()
        try:
            rank = self._client.my_rank()
        except ApiError as exc:
            log.warning("Rank unavailable: %s", exc)
            rank = None
        self._fv.reload()

        markets = [m for m in self._client.list_markets("open") if m.status is MarketStatus.OPEN]
        report.markets_scanned = len(markets)
        # Group each race's D / R / Independent markets so they share one cap.
        self._race = {p.market_id: race_key(p.market_title) for p in positions}
        self._race.update({m.id: race_key(m.title) for m in markets})

        owned = self._owned
        state = ExposureState.from_positions(
            cash, positions, self._lean_of, self._group_of, maker_exchanges=owned
        )
        holdings = {p.exchange_id: int(p.quantity) for p in positions if not p.settled}
        log.info(
            "Cycle start: cash=%.2f equity=%.2f positions=%d exposure=%.2f maker=%.2f "
            "net_d=%.2f rank=%s",
            cash,
            state.equity,
            len(holdings),
            state.total_cost,
            state.maker_cost,
            state.net_d,
            rank,
        )

        self._load_relationships()
        # New positions and arbitrage both need room under the buy-and-hold
        # cap; without it, fetching books for them only burns the rate limit.
        bh_room = self._s.max_total_exposure * state.equity - state.total_cost
        to_fetch = self._markets_to_fetch(markets, holdings, allow_open=bh_room >= 1.0)

        def progress(done: int, total: int) -> None:
            log.info("Scanning orderbooks: %d/%d", done, total)

        order_books = self._client.get_orderbooks(
            to_fetch, self._s.orderbook_depth, on_progress=progress
        )

        candidates: list[TradeCandidate] = []
        books: dict[str, OrderbookExchange] = {}
        for market in markets:
            book = order_books.get(market.id)
            if book is None:
                continue
            books.update({ex.exchange_id: ex for ex in book.exchanges})
            candidates.extend(self._signals.evaluate(market, book, holdings))
        candidates.extend(self._relationship_candidates(books))
        # Buy-and-hold stays out of exchanges the maker manages.
        if owned:
            candidates = [
                c for c in candidates if not any(leg.exchange_id in owned for leg in c.legs)
            ]
        self._cycles += 1
        report.candidates = len(candidates)

        candidates.sort(key=lambda c: (_PRIORITY[c.kind], -c.edge))
        scale = self._kelly_scale(rank)
        trades: list[SizedTrade] = []
        # Two arbitrage baskets sharing an exchange would compete for the same
        # top-of-book shares and leave one partly unhedged; keep the better one.
        arb_used: set[str] = set()
        for cand in candidates:
            if cand.kind == "arbitrage":
                legs = {leg.exchange_id for leg in cand.legs}
                if legs & arb_used:
                    log.debug("Skip overlapping arbitrage %s", cand.reason)
                    continue
            sized = self._risk.size(cand, state, scale)
            if sized is not None:
                trades.append(sized)
                if cand.kind == "arbitrage":
                    arb_used |= {leg.exchange_id for leg in cand.legs}

        maker_trades, quoted = self._maker_trades(
            tournament, markets, books, holdings, positions, candidates, state
        )
        report.maker_exchanges = len(quoted)

        report.orders_submitted = self._executor.execute(trades + maker_trades)
        report.traded = True
        if self._s.maker_enabled:
            self._update_owned(quoted, holdings, live)
        log.info(
            "Cycle done: markets=%d candidates=%d trades=%d maker_quotes=%d orders_accepted=%d",
            report.markets_scanned,
            report.candidates,
            len(trades),
            report.maker_exchanges,
            report.orders_submitted,
        )
        return report

    # -- market making ---------------------------------------------------
    def _maker_trades(
        self,
        tournament: TournamentSummary,
        markets: list[Market],
        books: dict[str, OrderbookExchange],
        holdings: dict[str, int],
        positions: list[Position],
        candidates: list[TradeCandidate],
        state: ExposureState,
    ) -> tuple[list[SizedTrade], set[str]]:
        if not self._s.maker_enabled:
            return [], set()
        end = parse_ts(tournament.end_date)
        if end is not None:
            hours_left = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left <= self._s.maker_stop_hours_before_end:
                log.info(
                    "Maker paused: %.1fh before tournament end (stops at %.0fh)",
                    hours_left,
                    self._s.maker_stop_hours_before_end,
                )
                return [], set()

        owned = self._owned
        # Stay out of exchanges buy-and-hold wants this cycle, and out of races
        # where buy-and-hold holds a position, so inventories never mix.
        busy = {leg.exchange_id for c in candidates for leg in c.legs}
        bh_races = {
            self._group_of(p.market_id)
            for p in positions
            if not p.settled and p.side is not None and p.exchange_id not in owned
        }

        tops = {eid: Top(q.best_bid, q.best_ask) for eid, q in self._bulk.items()}
        for eid, book in books.items():  # full books are at least as fresh
            b, a = book.best_bid(), book.best_ask()
            tops[eid] = Top(b.price if b else None, a.price if a else None)

        inputs: list[MakerInput] = []
        for m in markets:
            if m.is_composite:
                continue
            for ex in m.exchanges:
                eid = ex.id
                if eid in busy:
                    continue
                if eid not in owned and self._group_of(m.id) in bh_races:
                    continue
                fv = self._fv.estimate(eid)
                top = tops.get(eid)
                if fv is None or top is None:
                    continue
                inputs.append(MakerInput(eid, m.id, fv, top, holdings.get(eid, 0)))

        inventory = state.maker_cost  # held maker positions, before new quotes
        trades, quoted = self._maker.plan(inputs, state, owned)
        log.info(
            "Maker: %d eligible exchanges, quoting %d (inventory %.2f, new quotes reserve %.2f, "
            "budget %.2f)",
            len(inputs),
            len(quoted),
            inventory,
            state.maker_cost - inventory,
            self._s.maker_budget * state.equity,
        )
        return trades, quoted

    def _update_owned(self, quoted: set[str], holdings: dict[str, int], live: bool) -> None:
        """Remember quoted exchanges, and any still holding maker inventory."""
        kept = quoted | {eid for eid in self._owned if holdings.get(eid, 0)}
        if kept == self._owned:
            return
        if live:
            save_owned(self._s.maker_state_path, kept)
            self._owned = kept
        else:
            log.info("[DRY-RUN] maker would manage %d exchanges", len(kept))

    # -- run loop --------------------------------------------------------
    def run_forever(self) -> None:
        log.info("Starting engine (dry_run=%s, poll=%.0fs)", self._s.dry_run, self._s.poll_interval)
        try:
            while not self._stop:
                try:
                    self.run_cycle()
                except TermsNotAcknowledgedError as exc:
                    log.error("Blocked: accept the current documents on the site. %s", exc)
                    return
                except ResidenceUpdateRequiredError as exc:
                    log.error("Blocked: update your residence on the site. %s", exc)
                    return
                except AuthError as exc:
                    log.error("Blocked: API key rejected (need scopes read+trade). %s", exc)
                    return
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.exception("Cycle error (continuing): %s", exc)

                deadline = time.monotonic() + self._s.poll_interval
                while not self._stop and time.monotonic() < deadline:
                    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        finally:
            self._on_shutdown()
        log.info("Engine stopped.")

    def _on_shutdown(self) -> None:
        """Cancel resting orders when the loop exits so none linger unattended.

        Runs however the loop ended: Ctrl-C, a fatal error, or a clean stop.
        Skipped in dry-run (nothing was placed) or if disabled. Filled
        positions are kept; only unmatched bids/asks are cancelled.
        """
        if self._s.dry_run or not self._s.cancel_orders_on_exit:
            return
        try:
            cancelled = self._client.cancel_all()
            log.info("Shutdown: cancelled %d resting order(s).", cancelled)
        except Exception as exc:  # noqa: BLE001 - best effort on the way out
            log.error(
                "Shutdown: could not cancel resting orders: %s. "
                "Check open orders on the site manually.",
                exc,
            )
