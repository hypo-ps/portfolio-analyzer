from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import requests

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)

ACTION_BONUS = "BONUS"
ACTION_SPLIT = "SPLIT"
ACTION_DIVIDEND = "DIVIDEND"
ACTION_RIGHTS = "RIGHTS"
ACTION_BUYBACK = "BUYBACK"
ACTION_MERGER = "MERGER"
ACTION_CONSOLIDATION = "CONSOLIDATION"
ACTION_INTEREST = "INTEREST"
ACTION_OTHER = "OTHER"

PRICE_ADJUSTING_ACTIONS = frozenset({ACTION_BONUS, ACTION_SPLIT})

_BONUS_RE = re.compile(r"\bbonus[\s\-:]*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
_SPLIT_RE = re.compile(
    r"(?:face\s+value\s+split|stock\s+split|sub[-\s]*division)"
    r".*?from\s+(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)"
    r".*?to\s+(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CorpAction:
    isin: str
    ex_date: dt.date
    action_type: str
    ratio_num: float | None
    ratio_den: float | None
    price_factor: float
    raw_subject: str
    symbol: str
    name: str


def ca_url(start: dt.date, end: dt.date, *, index: str = "equities") -> tuple[str, dict[str, str]]:
    params = {
        "index": index,
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }
    return cfg.NSE_CA_API_URL, params


def fetch_ca_records(start: dt.date, end: dt.date) -> list[dict]:
    """Fetch corporate-action records from NSE's JSON API for [start, end]."""
    url, params = ca_url(start, end)
    headers = {
        "User-Agent": cfg.REFRESH_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": cfg.NSE_CA_REFERER,
    }
    log.info("Fetching corporate actions %s → %s", start.isoformat(), end.isoformat())
    resp = requests.get(url, params=params, headers=headers, timeout=cfg.NSE_CA_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected CA payload shape: {type(payload).__name__}")
    return payload


def _classify(subject: str) -> str:
    sl = subject.lower()
    if "face value split" in sl or "stock split" in sl or "sub-division" in sl:
        return ACTION_SPLIT
    if sl.lstrip().startswith("bonus") and _BONUS_RE.search(subject):
        return ACTION_BONUS
    if "consolidat" in sl:
        return ACTION_CONSOLIDATION
    if "dividend" in sl:
        return ACTION_DIVIDEND
    if "rights" in sl:
        return ACTION_RIGHTS
    if "buy" in sl and "back" in sl:
        return ACTION_BUYBACK
    if "merger" in sl or "demerger" in sl or "amalgam" in sl:
        return ACTION_MERGER
    if "interest" in sl:
        return ACTION_INTEREST
    return ACTION_OTHER


def _parse_ex_date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value or value == "-":
        return None
    try:
        return dt.datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError:
        return None


def _actions_for_subject(
    *, isin: str, ex_date: dt.date, subject: str, symbol: str, name: str,
) -> list[CorpAction]:
    """A single subject string may encode multiple events (e.g. Bonus + Split)."""
    out: list[CorpAction] = []
    bonus = _BONUS_RE.search(subject)
    split = _SPLIT_RE.search(subject)
    if bonus:
        x, y = float(bonus.group(1)), float(bonus.group(2))
        if y > 0 and x + y > 0:
            out.append(CorpAction(
                isin=isin, ex_date=ex_date, action_type=ACTION_BONUS,
                ratio_num=x, ratio_den=y, price_factor=y / (x + y),
                raw_subject=subject, symbol=symbol, name=name,
            ))
    if split:
        old_fv, new_fv = float(split.group(1)), float(split.group(2))
        if old_fv > 0 and new_fv > 0:
            out.append(CorpAction(
                isin=isin, ex_date=ex_date, action_type=ACTION_SPLIT,
                ratio_num=new_fv, ratio_den=old_fv, price_factor=new_fv / old_fv,
                raw_subject=subject, symbol=symbol, name=name,
            ))
    if out:
        return out
    # Fall back to classifier for non-price-adjusting actions
    return [CorpAction(
        isin=isin, ex_date=ex_date, action_type=_classify(subject),
        ratio_num=None, ratio_den=None, price_factor=1.0,
        raw_subject=subject, symbol=symbol, name=name,
    )]


def parse_ca_records(records: list[dict]) -> list[CorpAction]:
    out: list[CorpAction] = []
    for rec in records:
        isin = (rec.get("isin") or "").strip()
        subject = (rec.get("subject") or "").strip()
        ex_date = _parse_ex_date(rec.get("exDate", ""))
        if not isin or not subject or ex_date is None:
            continue
        out.extend(_actions_for_subject(
            isin=isin, ex_date=ex_date, subject=subject,
            symbol=(rec.get("symbol") or "").strip(),
            name=(rec.get("comp") or "").strip(),
        ))
    return out


def fetch_and_parse(start: dt.date, end: dt.date) -> list[CorpAction]:
    return parse_ca_records(fetch_ca_records(start, end))
