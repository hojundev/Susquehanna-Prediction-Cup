"""Fair-value estimation.

The bot only takes directional trades when it has an *independent*
probability estimate that differs from the market. Providers:

* ``StaticFairValueModel`` — a JSON file of ``exchangeId -> estimate`` that
  you maintain from your own research. Re-read every cycle.
* ``ExternalFairValueModel`` — live Kalshi / Polymarket prices for the
  exchanges you approved in ``mappings.json``.
* ``CompositeFairValueModel`` — combines the above; the first with a view wins.
* ``NullFairValueModel`` — no views; the bot runs arbitrage-only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .external import Quote
from .mapping import Mapping, load_mappings

log = logging.getLogger(__name__)

_LEANS = {"D", "R"}


@dataclass(frozen=True)
class FairValue:
    """Fair value for one exchange's YES side.

    ``confidence`` in [0, 1] scales sizing. ``lean`` tags which party a YES
    outcome favours (``"D"`` or ``"R"``) so the risk manager can cap net
    exposure to the national environment; untagged exchanges are not counted.
    """

    probability: float
    confidence: float = 0.5
    lean: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("probability must be in [0, 1]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.lean is not None and self.lean not in _LEANS:
            raise ValueError("lean must be 'D', 'R' or omitted")


class FairValueModel(Protocol):
    def estimate(self, exchange_id: str) -> FairValue | None:
        """Return a fair value for the exchange, or None if no view."""
        ...

    def reload(self) -> None:
        """Refresh estimates from their source."""
        ...


class NullFairValueModel:
    """No opinions: the bot runs arbitrage-only."""

    def estimate(self, exchange_id: str) -> FairValue | None:
        return None

    def reload(self) -> None:
        return None


class StaticFairValueModel:
    """Fair values loaded from a JSON file.

    Format (keys starting with ``_`` are ignored)::

        {
          "123": {"probability": 0.62, "confidence": 0.7, "lean": "D"},
          "456": 0.40
        }

    A bare number is a probability with default confidence and no lean. If
    the file becomes unreadable, the last good estimates are kept.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._values: dict[str, FairValue] = {}
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._mtime is None:
                log.warning("Fair-value file %s not found; no estimates loaded", self._path)
                self._mtime = -1.0
            return
        if mtime == self._mtime:
            return
        try:
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("top level must be a JSON object")
        except (OSError, ValueError) as exc:
            log.error("Failed to read fair-value file %s (keeping old values): %s", self._path, exc)
            return

        values: dict[str, FairValue] = {}
        for exchange_id, entry in raw.items():
            if exchange_id.startswith("_"):
                continue
            try:
                if isinstance(entry, dict):
                    lean = entry.get("lean")
                    values[exchange_id] = FairValue(
                        probability=float(entry["probability"]),
                        confidence=float(entry.get("confidence", 0.5)),
                        lean=str(lean).upper() if lean else None,
                    )
                else:
                    values[exchange_id] = FairValue(probability=float(entry))
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("Skipping bad fair-value entry %s: %s", exchange_id, exc)
        self._values = values
        self._mtime = mtime
        log.info("Loaded %d fair-value estimates from %s", len(values), self._path)

    def estimate(self, exchange_id: str) -> FairValue | None:
        return self._values.get(exchange_id)


class QuoteFeed(Protocol):
    source: str

    def quotes(self, refs: Iterable[str]) -> dict[str, Quote]: ...


@dataclass(frozen=True)
class ExternalParams:
    max_spread: float = 0.08
    max_disagreement: float = 0.05
    require_both: bool = False
    single_confidence: float = 0.5
    dual_confidence: float = 0.8


@dataclass
class ExternalView:
    """What the external model concluded for one exchange (for logs and `check`)."""

    fair: FairValue | None
    quotes: dict[str, Quote]
    note: str


class ExternalFairValueModel:
    """Fair values from Kalshi / Polymarket prices for approved mappings.

    Each :meth:`reload` (once per cycle) re-reads the mapping file if it
    changed and fetches fresh quotes. For each approved exchange:

    * quotes wider than ``max_spread`` are ignored (too thin to trust);
    * with two sources, the mids must agree within ``max_disagreement``,
      otherwise the exchange gets no fair value this cycle;
    * the fair value is the volume-weighted mid (equal weights if no volume);
    * confidence is ``dual_confidence`` with two sources, ``single_confidence``
      with one (or no value at all when ``require_both``), reduced as spreads
      widen.

    If a source fails to respond, it contributes nothing that cycle. Stale
    prices are never reused.
    """

    def __init__(
        self, mappings_path: str | Path, feeds: list[QuoteFeed], params: ExternalParams
    ) -> None:
        self._path = Path(mappings_path)
        self._feeds = feeds
        self._p = params
        self._mtime: float | None = None
        self._mappings: dict[str, Mapping] = {}
        self.views: dict[str, ExternalView] = {}

    @property
    def mappings(self) -> dict[str, Mapping]:
        return self._mappings

    def _load_mappings(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._mtime is None:
                log.warning("Mapping file %s not found; no external fair values", self._path)
                self._mtime = -1.0
            self._mappings = {}
            return
        if mtime == self._mtime:
            return
        try:
            all_maps = load_mappings(self._path)
        except (OSError, ValueError) as exc:
            log.error("Bad mapping file %s (keeping previous): %s", self._path, exc)
            return
        self._mappings = {
            k: m for k, m in all_maps.items() if m.approved and not m.rejected and m.refs
        }
        self._mtime = mtime
        log.info(
            "Loaded %d approved mappings (of %d) from %s",
            len(self._mappings),
            len(all_maps),
            self._path,
        )

    def reload(self) -> None:
        self._load_mappings()
        fetched: dict[str, dict[str, Quote]] = {}
        for feed in self._feeds:
            refs = {
                m.refs[feed.source].ref for m in self._mappings.values() if feed.source in m.refs
            }
            if not refs:
                continue
            try:
                fetched[feed.source] = feed.quotes(refs)
            except Exception as exc:  # noqa: BLE001 - a dead feed must not stop trading
                log.warning("%s quotes unavailable this cycle: %s", feed.source, exc)
        self.views = {eid: self._combine(m, fetched) for eid, m in self._mappings.items()}
        usable = sum(1 for v in self.views.values() if v.fair)
        log.info("External fair values: %d usable of %d mapped", usable, len(self.views))

    def _combine(self, m: Mapping, fetched: dict[str, dict[str, Quote]]) -> ExternalView:
        quotes: dict[str, Quote] = {}
        for src, ref in m.refs.items():
            q = fetched.get(src, {}).get(ref.ref)
            if q is None:
                continue
            q = q.inverted() if ref.invert else q
            if q.spread > self._p.max_spread:
                continue
            quotes[src] = q
        if not quotes:
            return ExternalView(None, quotes, "no usable quote")
        if len(quotes) == 1 and (self._p.require_both and len(m.refs) > 1):
            return ExternalView(None, quotes, "only one source quoted")
        mids = [q.mid for q in quotes.values()]
        if max(mids) - min(mids) > self._p.max_disagreement:
            return ExternalView(None, quotes, f"sources disagree ({min(mids):.3f}-{max(mids):.3f})")

        weights = [max(q.volume_24h, 0.0) for q in quotes.values()]
        if sum(weights) <= 0:
            weights = [1.0] * len(quotes)
        prob = sum(w * q.mid for w, q in zip(weights, quotes.values(), strict=True)) / sum(weights)
        base = self._p.dual_confidence if len(quotes) > 1 else self._p.single_confidence
        worst_spread = max(q.spread for q in quotes.values())
        conf = base * max(0.0, 1.0 - worst_spread / (2 * self._p.max_spread))
        fair = FairValue(min(1.0, max(0.0, prob)), round(conf, 3), m.lean)
        return ExternalView(fair, quotes, f"{len(quotes)} source(s)")

    def estimate(self, exchange_id: str) -> FairValue | None:
        view = self.views.get(exchange_id)
        return view.fair if view else None


class CompositeFairValueModel:
    """Asks each model in order; the first with a view wins.

    Put your hand-written static file first so your own views override
    the external feed for any exchange you list in both.
    """

    def __init__(self, models: list[FairValueModel]) -> None:
        self._models = models

    def estimate(self, exchange_id: str) -> FairValue | None:
        for m in self._models:
            fv = m.estimate(exchange_id)
            if fv is not None:
                return fv
        return None

    def reload(self) -> None:
        for m in self._models:
            m.reload()
