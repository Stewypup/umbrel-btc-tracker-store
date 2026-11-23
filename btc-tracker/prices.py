"""
prices.py — Daily BTC prices in USD and CAD.

Sources (both free, no API key):
  * USD: Bitstamp daily BTC/USD candles (closing price of each UTC day).
  * CAD: the USD price converted with the Bank of Canada's official daily
    USD/CAD rate (Valet API, series FXUSDCAD). Weekends and holidays use the
    last business day's rate; today uses the latest published rate.

backfill_prices() makes sure every day from the oldest wallet transaction
(or one year back, whichever is earlier) to today has a price. It only
downloads what's missing, plus the last few days so today's price stays
current.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import requests

import db

log = logging.getLogger(__name__)

BITSTAMP_OHLC = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
BOC_FX = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
REQUEST_TIMEOUT = 30
PAGE_DAYS = 1000          # Bitstamp returns at most 1000 candles per call
REFRESH_DAYS = 3          # always re-fetch the last few days
MIN_HISTORY_DAYS = 365    # keep at least a year of prices for the chart
SOURCE = "bitstamp+boc"   # stored in settings; a change triggers a full refetch

USER_AGENT = "cold-storage-tracker (self-hosted)"


class PriceError(Exception):
    """A price source couldn't be reached or returned something unexpected."""


def _utc_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


def _get_json(url, params):
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        host = url.split("/")[2]
        raise PriceError(f"Couldn't get prices from {host} ({exc}).") from exc
    except ValueError as exc:
        raise PriceError(f"Unexpected response from {url}.") from exc


def fetch_usd(start, end):
    """Daily BTC/USD closing prices from Bitstamp. Returns {date_str: price}."""
    out = {}
    cursor = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    stop = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    while cursor <= stop:
        page_end = min(cursor + timedelta(days=PAGE_DAYS - 1), stop)
        data = _get_json(BITSTAMP_OHLC, {
            "step": 86400,
            "limit": PAGE_DAYS,
            "start": int(cursor.timestamp()),
            "end": int(page_end.timestamp()) + 86399,
        })
        try:
            candles = data["data"]["ohlc"]
        except (KeyError, TypeError) as exc:
            raise PriceError(f"Unexpected Bitstamp response: {str(data)[:200]}") from exc
        for c in candles:
            d = _utc_date(c["timestamp"])
            if start <= d <= end:
                out[d.isoformat()] = round(float(c["close"]), 2)
        cursor = page_end + timedelta(days=1)
    return out


def fetch_usdcad(start, end):
    """
    Bank of Canada USD/CAD rate for every calendar day from start to end,
    forward-filled over weekends and holidays. Returns {date_str: rate}.
    """
    # Look back a couple of weeks so the first days have a rate to carry
    data = _get_json(BOC_FX, {
        "start_date": (start - timedelta(days=14)).isoformat(),
        "end_date": end.isoformat(),
    })
    obs = {}
    for o in data.get("observations", []):
        try:
            obs[o["d"]] = float(o["FXUSDCAD"]["v"])
        except (KeyError, TypeError, ValueError):
            continue
    if not obs:
        raise PriceError("The Bank of Canada returned no USD/CAD rates.")

    # The series starts in 2017; use its first rate for anything earlier.
    first_rate = obs[min(obs)]
    if start.isoformat() < min(obs):
        log.warning("No Bank of Canada rate before %s; using %.4f for earlier "
                    "CAD prices.", min(obs), first_rate)

    out = {}
    rate = first_rate
    d = start - timedelta(days=14)
    while d <= end:
        rate = obs.get(d.isoformat(), rate)
        if d >= start:
            out[d.isoformat()] = rate
        d += timedelta(days=1)
    return out


def _wanted_start(conn):
    """Earliest date we need a price for."""
    oldest = db.get_oldest_transaction_date(conn)
    year_ago = date.today() - timedelta(days=MIN_HISTORY_DAYS)
    if oldest:
        return min(date.fromisoformat(oldest), year_ago)
    return year_ago


def backfill_prices():
    """
    Fill in any missing daily prices from the oldest transaction to today,
    and refresh the last few days. Raises PriceError if a source fails.
    """
    today = datetime.now(timezone.utc).date()

    with db.get_conn() as conn:
        start = _wanted_start(conn)
        if db.get_setting(conn, "price_source") != SOURCE:
            have = set()          # new source: refetch everything once
            log.info("Price source changed to %s; fetching full history.", SOURCE)
        else:
            have = db.get_complete_price_dates(conn)

    # First day in the wanted range that has no price yet
    first_missing = None
    d = start
    while d <= today:
        if d.isoformat() not in have:
            first_missing = d
            break
        d += timedelta(days=1)

    fetch_from = today - timedelta(days=REFRESH_DAYS - 1)
    if first_missing and first_missing < fetch_from:
        fetch_from = first_missing

    log.info("Fetching BTC prices %s → %s", fetch_from, today)
    usd = fetch_usd(fetch_from, today)
    if not usd:
        raise PriceError("Bitstamp returned no BTC prices.")
    fx = fetch_usdcad(fetch_from, today)

    with db.get_conn() as conn:
        for d_str in sorted(usd):
            rate = fx.get(d_str)
            cad = round(usd[d_str] * rate, 2) if rate else None
            db.upsert_price(conn, d_str, usd[d_str], cad)
        db.set_setting(conn, "price_source", SOURCE)

    log.info("Price data updated: %d days (%s → %s).",
             len(usd), min(usd), max(usd))
    return len(usd)
