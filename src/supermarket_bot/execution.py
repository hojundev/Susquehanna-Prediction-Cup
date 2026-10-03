"""Order execution: the only place that submits (or simulates) orders.

* Arbitrage baskets go through ``/orders/multi-leg`` so either every leg is
  accepted or none is. Any leg left resting is cancelled straight away, since
  an unfilled leg leaves the basket unhedged.
* Everything else goes through ``/orders/batch`` (best-effort, 50 per call).
* ``dry_run`` logs the exact intents and submits nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ApiError, SuperMarketClient
from .models import OrderIntent
from .risk import SizedTrade

log = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, client: SuperMarketClient, dry_run: bool, ttl_seconds: int) -> None:
        self._client = client
        self._dry_run = dry_run
        self._ttl = ttl_seconds

    def execute(self, trades: list[SizedTrade]) -> int:
        """Submit trades; returns the number of orders the API accepted."""
        if self._dry_run:
            n = 0
            for t in trades:
                for i in t.intents:
                    log.info(
                        "[DRY-RUN] %s: %s %s %s qty=%d @%.3f (%s)",
                        t.kind,
                        i.action.value,
                        i.side.value,
                        i.exchange_id,
                        i.quantity,
                        i.price,
                        i.reason,
                    )
                    n += 1
            return n

        accepted = 0
        for t in trades:
            if t.atomic:
                accepted += self._execute_atomic(t.intents)
        singles = [i for t in trades if not t.atomic for i in t.intents]
        for start in range(0, len(singles), 50):
            accepted += self._execute_batch(singles[start : start + 50])
        return accepted

    def _execute_atomic(self, intents: list[OrderIntent]) -> int:
        try:
            legs = self._client.place_multi_leg(intents, self._ttl)
        except ApiError as exc:
            log.error("Atomic basket rejected (nothing placed): %s", exc)
            return 0
        resting = [leg for leg in legs if leg.get("open") and leg.get("orderId") is not None]
        if resting:
            log.warning(
                "%d of %d basket legs did not fully fill; cancelling remainders. "
                "Filled legs are kept and may be partly unhedged.",
                len(resting),
                len(legs),
            )
            for leg in resting:
                self._cancel_quietly(int(leg["orderId"]))
        return len(legs)

    def _execute_batch(self, intents: list[OrderIntent]) -> int:
        try:
            results = self._client.place_batch(intents, self._ttl)
        except ApiError as exc:
            log.error("Batch of %d failed: %s", len(intents), exc)
            return 0
        ok = 0
        for r in results:
            if r.get("ok"):
                ok += 1
                continue
            idx = r.get("index")
            data: Any = r.get("data") or {}
            err = data.get("error", data) if isinstance(data, dict) else data
            intent = intents[idx] if isinstance(idx, int) and idx < len(intents) else None
            log.error("Order rejected (%s): %s | %s", r.get("status"), err, intent)
        return ok

    def _cancel_quietly(self, order_id: int) -> None:
        try:
            self._client.cancel_order(order_id)
        except ApiError as exc:
            log.warning("Could not cancel order %s: %s", order_id, exc)
