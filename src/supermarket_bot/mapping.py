"""Mapping Super Market exchanges to Kalshi / Polymarket contracts.

The mapping file (``mappings.json``) is keyed by Super Market exchange id::

    {
      "12345": {
        "title": "Which party wins the House? — Democratic Party",
        "approved": true,
        "lean": "D",
        "kalshi":     {"ref": "CONTROLH-2026-D", "invert": false},
        "polymarket": {"ref": "562802",          "invert": false}
      }
    }

* Only entries with ``"approved": true`` are ever traded.
* ``invert: true`` means Super Market YES is the external contract's NO
  (for example "Will Republicans win the House?" mapped to a Democrats ticker).
* ``suggest`` writes new entries with ``approved: false`` and never changes
  an entry you have already approved.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .external import Candidate

log = logging.getLogger(__name__)

SOURCES = ("kalshi", "polymarket")


@dataclass(frozen=True)
class SourceRef:
    ref: str
    invert: bool = False


@dataclass
class Mapping:
    exchange_id: str
    title: str = ""
    approved: bool = False
    lean: str | None = None
    refs: dict[str, SourceRef] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # suggestions, notes: kept as-is
    #: Reviewed and found to have no correct match. `suggest` and `approve`
    #: leave rejected entries alone, and they are never traded.
    rejected: bool = False

    @classmethod
    def from_json(cls, exchange_id: str, raw: dict[str, Any]) -> Mapping:
        refs: dict[str, SourceRef] = {}
        for src in SOURCES:
            r = raw.get(src)
            if isinstance(r, dict) and r.get("ref"):
                refs[src] = SourceRef(str(r["ref"]), bool(r.get("invert", False)))
        lean = raw.get("lean")
        lean = str(lean).upper() if lean else None
        known = {"title", "approved", "rejected", "lean", *SOURCES}
        rejected = raw.get("rejected") is True
        return cls(
            exchange_id=exchange_id,
            title=str(raw.get("title", "")),
            approved=raw.get("approved") is True and not rejected,
            lean=lean if lean in ("D", "R") else None,
            refs=refs,
            extra={k: v for k, v in raw.items() if k not in known},
            rejected=rejected,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "approved": self.approved}
        if self.rejected:
            out["rejected"] = True
        if self.lean:
            out["lean"] = self.lean
        for src in SOURCES:
            r = self.refs.get(src)
            out[src] = {"ref": r.ref, "invert": r.invert} if r else None
        out.update(self.extra)
        return out


def lean_from_title(title: str) -> str | None:
    """Infer D/R lean from a party in the title; None if ambiguous."""
    t = title.lower()
    dem = any(w in t for w in ("democrat", "democratic"))
    rep = any(w in t for w in ("republican", " gop"))
    if dem and not rep:
        return "D"
    if rep and not dem:
        return "R"
    return None


def top_suggestion_score(entry: Mapping, source: str) -> float:
    """Best match score for a source, parsed from the stored suggestions text.

    ``suggest`` records lines like ``"0.71  REF  Title  [bid/ask]"``. Returns
    0.0 when there is no suggestion for that source.
    """
    lines = (entry.extra.get("suggestions") or {}).get(source) or []
    best = 0.0
    for line in lines:
        try:
            best = max(best, float(line.split()[0]))
        except (ValueError, IndexError):
            continue
    return best


def load_mappings(path: str | Path) -> dict[str, Mapping]:
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level must be a JSON object")
    return {
        k: Mapping.from_json(k, v)
        for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, dict)
    }


def save_mappings(path: str | Path, mappings: dict[str, Mapping]) -> None:
    p = Path(path)
    data: dict[str, Any] = {
        "_help": (
            "Super Market exchangeId -> external contracts. Review each entry, fix refs or set "
            "invert if YES means the opposite there, then set approved: true. Only approved "
            "entries are traded. Delete 'suggestions' once reviewed if you like."
        )
    }
    data.update({k: m.to_json() for k, m in sorted(mappings.items())})
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}
_SYNONYMS = {
    "democrats": "democrat",
    "democratic": "democrat",
    "dem": "democrat",
    "dems": "democrat",
    "republicans": "republican",
    "gop": "republican",
    "rep": "republican",
    "reps": "republican",
    "governorship": "governor",
    "gubernatorial": "governor",
    "senator": "senate",
    "congressional": "house",
    "representatives": "house",
    "win": "wins",
    "won": "wins",
    "winner": "wins",
    "control": "controls",
}
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "will",
    "be",
    "to",
    "for",
    "and",
    "or",
    "who",
    "which",
    "is",
    "on",
    "after",
    "party",
    "election",
    "elections",
    "race",
    "midterm",
    "midterms",
    "general",
    "us",
    "u",
    "s",
    "united",
    "states",
    "by",
    "at",
    "yes",
    "no",
}


def _states_in(text: str) -> set[str]:
    t = f" {text.lower()} "
    found = {s for s in _STATES if f" {s} " in t}
    # "West Virginia" also contains "Virginia"; keep the longer name only.
    return {s for s in found if not any(s != o and s in o for o in found)}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d+", text.lower())
    return {_SYNONYMS.get(w, w) for w in words if w not in _STOP}


_STATE_LEG = re.compile(r"\b(state (senate|house|assembly)|legislature|house of delegates)\b")
#: "by 0%-5%", "by 5-10%", "within 5", "margin" — a bracket, not a plain winner.
_MARGIN = re.compile(r"\bby\s+\d|\bwithin\s+\d|\bmargin\b|\d\s*%\s*-\s*\d|\d\s*-\s*\d\s*%")
#: Two-letter postal code + district number: "AL-02", "AL02", "FL 7".
_DISTRICT = re.compile(r"\b([A-Za-z]{2})[\s-]?(\d{1,2})\b")
#: Combo/multi-seat Kalshi tickers that aren't a single race (e.g. KXFIVESTATESEN).
_COMBO = re.compile(r"\b(two|three|four|five|six)state|trifecta|combo|fivestate|fourstate", re.I)

_POSTAL = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}
_POSTAL_CODES = set(_POSTAL.values())


def _district(text: str) -> str | None:
    """Return a normalized state-district key like ``"al-02"`` if present.

    Handles both ``AL-02`` (Super Market titles) and ``KXHOUSERACE-AL02-26-R``
    (Kalshi refs). Only accepts real postal codes to avoid matching noise.
    """
    for st, num in _DISTRICT.findall(text):
        st = st.lower()
        if st in _POSTAL_CODES:
            return f"{st}-{int(num):02d}"
    return None


#: Office named in a title, most specific first ("Lieutenant Governor" before
#: "Governor"). Used as a hard gate so a Senate market never matches an
#: Attorney General contract.
_OFFICES = (
    ("ltgov", re.compile(r"\blieutenant governor\b|\blt\.?\s*gov")),
    ("ag", re.compile(r"\battorney general\b")),
    ("sos", re.compile(r"\bsecretary of state\b")),
    ("governor", re.compile(r"\bgovernor|\bgubernatorial\b")),
    ("senate", re.compile(r"\bsenate\b|\bsenator")),
    ("house", re.compile(r"\bhouse\b|\bcongress|\brepresentatives?\b")),
)
#: Office from a Kalshi ticker prefix, when the title names none.
_TICKER_OFFICES = (
    ("LTGOV", "ltgov"),
    ("ATTYGEN", "ag"),
    ("GOV", "governor"),
    ("SENATE", "senate"),
    ("CONTROLS", "senate"),
    ("HOUSE", "house"),
    ("CONTROLH", "house"),
)
#: State code right after the office in a Kalshi ticker: SENATELA-26-R -> LA.
_TICKER_STATE = re.compile(r"^(?:KX)?(?:GOVPARTY|ATTYGEN|LTGOV|SENATE|HOUSE|GOV)([A-Z]{2})")
#: Two-digit year segment in a Kalshi ticker: "-28-", "-26NOV-", "-26NOV03-".
_TICKER_YEAR = re.compile(r"(\d{2})(?:[A-Z]{3}\d{0,2})?")


def _office(text: str, ref: str) -> str | None:
    t = text.lower()
    for name, pattern in _OFFICES:
        if pattern.search(t):
            return name
    r = ref.upper().removeprefix("KX")
    for prefix, name in _TICKER_OFFICES:
        if r.startswith(prefix):
            return name
    return None


def _ticker_state(ref: str) -> str | None:
    m = _TICKER_STATE.match(ref.upper())
    if m and m.group(1).lower() in _POSTAL_CODES:
        return m.group(1).lower()
    return None


def _ticker_years(ref: str) -> set[str]:
    years: set[str] = set()
    for segment in ref.upper().split("-")[1:]:
        m = _TICKER_YEAR.fullmatch(segment)
        if m:
            years.add("20" + m.group(1))
    return years


@dataclass(frozen=True)
class _Features:
    tokens: frozenset[str]
    joined: str
    years: frozenset[str]
    states: frozenset[str]
    state_leg: bool  # a state legislature chamber, not Congress
    is_margin: bool  # a win-by-margin bracket, not a plain winner market
    district: str | None  # "al-02" for a House race, else None
    is_combo: bool  # a multi-seat / combo contract, not one race
    office: str | None = None  # "senate", "governor", "ag", ...
    ref_state: str | None = None  # postal code embedded in a Kalshi ticker


def _features(text: str, ref: str = "") -> _Features:
    toks = frozenset(_tokens(text))
    # The ref carries the authoritative state-district code; the title may too.
    district = _district(text) or _district(ref)
    # A state named in the title also implies its postal code for district use.
    if district is None:
        states = _states_in(text)
        codes = {_POSTAL[s] for s in states}
        for st, num in _DISTRICT.findall(text):
            if st.lower() in codes or st.lower() in _POSTAL_CODES:
                district = f"{st.lower()}-{int(num):02d}"
                break
    return _Features(
        tokens=toks,
        joined=" ".join(sorted(toks)),
        years=frozenset(re.findall(r"\b20\d\d\b", text + " " + ref)) | _ticker_years(ref),
        states=frozenset(_states_in(text)),
        state_leg=bool(_STATE_LEG.search(text.lower())),
        is_margin=bool(_MARGIN.search(text.lower())),
        district=district,
        is_combo=bool(_COMBO.search(text)) or bool(_COMBO.search(ref)),
        office=_office(text, ref),
        ref_state=_ticker_state(ref),
    )


def _score(
    a: _Features, b: _Features, *, exact: bool = True, target_year: str | None = None
) -> float:
    """Score candidate ``b`` against query ``a``.

    ``target_year`` is the tournament's election year. Super Market titles
    carry no year, so when the query has none we lean on this to prefer the
    right cycle (e.g. 2026 over 2028) instead of letting word overlap decide.
    """
    if a.years and b.years and not a.years & b.years:
        return 0.0
    if a.states and b.states and not a.states & b.states:
        return 0.0
    # Hard gate on House districts: a district query matches only the exact
    # same district. This blocks AL-02 -> MO-02 and FL-07 -> FL-10 mismatches.
    if (a.district or b.district) and a.district != b.district:
        return 0.0
    # A combo / multi-seat contract is never a single-race match.
    if b.is_combo and not a.is_combo:
        return 0.0
    # Different offices are different races: Senate vs Attorney General,
    # Governor vs Lieutenant Governor, state House vs U.S. Senate.
    if a.office and b.office and a.office != b.office:
        return 0.0
    # The state inside a Kalshi ticker must be the query's state, even when
    # the contract's title doesn't name one (blocks Kentucky -> SENATELA).
    if b.ref_state and a.states and b.ref_state not in {_POSTAL[s] for s in a.states}:
        return 0.0
    if not a.tokens or not b.tokens:
        return 0.0
    score = len(a.tokens & b.tokens) / len(a.tokens | b.tokens)
    if exact:
        seq = SequenceMatcher(None, a.joined, b.joined).ratio()
        score = 0.7 * score + 0.3 * seq
    # A state race vs a national contract, or a state legislature vs Congress,
    # are different questions even when the words overlap.
    if bool(a.states) != bool(b.states):
        score *= 0.5
    if a.state_leg != b.state_leg:
        score *= 0.5
    # A plain winner query must not match a win-by-margin bracket.
    if a.is_margin != b.is_margin:
        score *= 0.25
    # Prefer the tournament's election year when the candidate names one.
    if target_year and b.years:
        score *= 1.0 if target_year in b.years else 0.3
    return round(score, 3)


def similarity(a: str, b: str, target_year: str | None = None, b_ref: str = "") -> float:
    """0-1 score. Zero when years, states or House districts clearly differ."""
    return _score(_features(a), _features(b, b_ref), target_year=target_year)


class CandidateIndex:
    """Pre-tokenised candidates so tens of thousands of contracts rank quickly."""

    def __init__(self, candidates: Iterable[Candidate]) -> None:
        self._items = [(c, _features(c.text, c.ref)) for c in candidates]

    def __len__(self) -> int:
        return len(self._items)

    def rank(
        self, text: str, top: int = 3, shortlist: int = 40, target_year: str | None = None
    ) -> list[tuple[float, Candidate]]:
        q = _features(text)
        # Cheap token score first; the slower sequence ratio only on the shortlist.
        rough = [(_score(q, f, exact=False, target_year=target_year), c, f) for c, f in self._items]
        rough = [r for r in rough if r[0] > 0]
        rough.sort(key=lambda r: -r[0])
        scored = [(_score(q, f, target_year=target_year), c) for _, c, f in rough[:shortlist]]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: -s[0])
        return scored[:top]


def rank_candidates(
    text: str,
    candidates: Iterable[Candidate] | CandidateIndex,
    top: int = 3,
    target_year: str | None = None,
) -> list[tuple[float, Candidate]]:
    index = candidates if isinstance(candidates, CandidateIndex) else CandidateIndex(candidates)
    return index.rank(text, top, target_year=target_year)
