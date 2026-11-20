"""
sync.py — Runs the full sync (wallet scan → balances → prices) and keeps
track of its progress so the UI can show a progress bar.

Only one sync runs at a time: pressing Sync Now while the daily job (or
another browser tab's sync) is running just attaches to the running one.

State lives in memory, which works because gunicorn runs a single worker.
"""

import logging
import threading
import time

import db
import prices
import scanner
import balances

log = logging.getLogger(__name__)

# Share of the progress bar given to each stage (adds up to 1.0)
W_SCAN = 0.85
W_BALANCES = 0.05
W_PRICES = 0.10

_lock = threading.Lock()
_state = {
    "sync_id": 0,          # increments every time a sync starts
    "running": False,
    "source": None,        # "manual" or "scheduled"
    "stage": "idle",       # idle | scanning | balances | prices | done
    "percent": 0.0,
    "message": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "wallets_total": 0,
    "wallets_done": 0,
}


def status():
    """Snapshot of the current sync state (safe to JSON-encode)."""
    with _lock:
        s = dict(_state)
    s["percent"] = round(s["percent"], 1)
    return s


def _update(**kw):
    with _lock:
        if "percent" in kw:
            # Never let the bar move backwards
            kw["percent"] = max(_state["percent"], min(kw["percent"], 100.0))
        _state.update(kw)


def _claim(source):
    """Mark a sync as running. Returns False if one is already running."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(
            sync_id=_state["sync_id"] + 1,
            running=True,
            source=source,
            stage="scanning",
            percent=0.0,
            message="Starting sync…",
            error=None,
            started_at=time.time(),
            finished_at=None,
            wallets_total=0,
            wallets_done=0,
        )
        return True


def start(source="manual"):
    """Start a sync in a background thread. Returns False if one is running."""
    if not _claim(source):
        return False
    threading.Thread(target=_run, daemon=True, name="sync").start()
    return True


def run_blocking(source="scheduled"):
    """Run a sync in the calling thread (used by the daily scheduler)."""
    if not _claim(source):
        log.info("Sync already running; skipping %s sync.", source)
        return
    _run()


def _run():
    errors = []
    try:
        with db.get_conn() as conn:
            wallets = [dict(w) for w in db.get_wallets(conn)]
        n = len(wallets)
        _update(wallets_total=n)

        # ---- 1. Scan wallets ------------------------------------------
        if n:
            estimates = {w["id"]: scanner.estimate_address_count(w["id"])
                         for w in wallets}
            total_est = sum(estimates.values())
            done_est = 0  # estimated addresses in wallets already finished

            for i, w in enumerate(wallets):
                est = estimates[w["id"]]
                label = w["label"]
                _update(message=f"Scanning {label} ({i + 1} of {n})…")

                def on_progress(checked, txs, est=est, label=label, i=i,
                                done_est=done_est):
                    # Cap within-wallet progress just short of the wallet's
                    # share, since the gap limit can stretch a scan.
                    part = min(checked, est * 0.97)
                    _update(
                        percent=100 * W_SCAN * (done_est + part) / total_est,
                        message=(
                            f"Scanning {label} ({i + 1} of {n}) — "
                            f"{checked} addresses checked, "
                            f"{txs} transaction{'s' if txs != 1 else ''} found"
                        ),
                    )

                try:
                    scanner.scan_wallet(w["id"], w["xpub"], progress=on_progress)
                except scanner.ServerUnavailable as exc:
                    log.error("Sync stopped: %s", exc)
                    errors.append(str(exc))
                    break  # no point trying the other wallets
                except Exception as exc:
                    log.exception("Error scanning wallet %s", w["id"])
                    errors.append(f"{label}: {exc}")

                done_est += est
                _update(
                    wallets_done=i + 1,
                    percent=100 * W_SCAN * done_est / total_est,
                )

        # ---- 2. Rebuild daily balances --------------------------------
        _update(stage="balances", percent=100 * W_SCAN,
                message="Rebuilding balance history…")
        for i, w in enumerate(wallets):
            try:
                balances.rebuild_balances_for_wallet(w["id"])
            except Exception as exc:
                log.exception("Error rebuilding balances for wallet %s", w["id"])
                errors.append(f"{w['label']} balances: {exc}")
            _update(percent=100 * (W_SCAN + W_BALANCES * (i + 1) / n))

        # ---- 3. Update BTC prices -------------------------------------
        _update(stage="prices", percent=100 * (W_SCAN + W_BALANCES),
                message="Updating BTC prices…")
        try:
            prices.backfill_prices()
        except Exception as exc:
            log.exception("Price update failed")
            errors.append(f"Price update: {exc}")

    except Exception as exc:
        log.exception("Sync failed")
        errors.append(str(exc))

    finally:
        took = time.time() - (_state["started_at"] or time.time())
        if errors:
            msg = "Sync finished with problems."
        else:
            msg = f"Sync complete in {took:.0f}s."
        with _lock:
            _state.update(
                running=False,
                stage="done",
                percent=100.0,
                message=msg,
                error=" ".join(errors) if errors else None,
                finished_at=time.time(),
            )
        log.info("%s %s", msg, "; ".join(errors))
