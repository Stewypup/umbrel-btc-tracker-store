"""
app.py — Flask application factory and route definitions.
"""

import csv
import datetime
import io
import json
import logging
import os
import threading

from flask import Flask, redirect, render_template, request, url_for, flash, jsonify, Response

import db
import prices
import scanner
import balances
import scheduler
import sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

    # Initialise DB schema
    db.init_db()

    # Start background scheduler
    scheduler.start()

    # On first start, backfill prices in the background so the UI loads fast
    def _initial_sync():
        log.info("Running initial price backfill…")
        try:
            prices.backfill_prices()
        except Exception:
            log.exception("Initial price backfill failed (will retry on next sync)")

    t = threading.Thread(target=_initial_sync, daemon=True)
    t.start()

    # ------------------------------------------------------------------ #
    #  Routes                                                              #
    # ------------------------------------------------------------------ #

    @app.route("/")
    def dashboard():
        with db.get_conn() as conn:
            # Read user's currency preference (default to USD)
            currency = db.get_setting(conn, "currency", "usd")
            currency_label = "CAD" if currency == "cad" else "USD"
            show_obsolete = db.get_setting(conn, "show_obsolete", "0") == "1"

            wallets = db.get_wallets(conn)
            latest_price = db.get_latest_price(conn, currency)

            wallet_data = []
            total_sats = 0
            for w in wallets:
                bal_sats = db.get_current_balance_sats(conn, w["id"])
                # Totals include every wallet, so funds are never hidden
                total_sats += bal_sats
                bal_btc = bal_sats / 1e8
                bal_fiat = round(bal_btc * latest_price, 2) if latest_price else None
                avg_price = db.get_avg_purchase_price(conn, w["id"], currency)
                wallet_data.append({
                    "id": w["id"],
                    "label": w["label"],
                    "xpub_short": w["xpub"][:20] + "...",
                    "obsolete": bool(w["obsolete"]),
                    "balance_sats": bal_sats,
                    "balance_fiat": bal_fiat,
                    "avg_price": avg_price,
                    "last_scanned_at": w["last_scanned_at"],
                })

            obsolete_count = sum(1 for w in wallet_data if w["obsolete"])
            active_count = len(wallet_data) - obsolete_count
            shown_wallets = [w for w in wallet_data
                             if show_obsolete or not w["obsolete"]]

            total_btc = round(total_sats / 1e8, 8)
            total_fiat = round(total_btc * latest_price, 2) if latest_price else None

            # Portfolio chart data (fiat value over time)
            chart_data = db.get_portfolio_chart_data(conn, currency)

            # Cumulative amount invested (real purchases only, no transfers)
            invested_data = db.get_cumulative_invested(conn, currency)

            # Individual purchases, for the chart markers
            purchases = db.get_purchases(conn, currency)

            # Overall cost basis across every wallet, obsolete included
            cost_basis = db.get_overall_cost_basis(conn, currency)

            # All-time-high portfolio value
            max_portfolio = db.get_max_portfolio_value(conn, currency)

        return render_template(
            "dashboard.html",
            wallets=shown_wallets,
            active_count=active_count,
            obsolete_count=obsolete_count,
            show_obsolete=show_obsolete,
            total_btc=total_btc,
            total_fiat=total_fiat,
            latest_price=latest_price,
            currency=currency,
            currency_label=currency_label,
            max_portfolio=max_portfolio,
            cost_basis=cost_basis,
            chart_labels=json.dumps([r["date"] for r in chart_data]),
            chart_values=json.dumps([r["total_fiat"] for r in chart_data]),
            chart_prices=json.dumps([r["price"] for r in chart_data]),
            invested_labels=json.dumps([r["date"] for r in invested_data]),
            invested_values=json.dumps([r["cumulative_invested"] for r in invested_data]),
            purchases=json.dumps(purchases),
        )

    @app.route("/wallets")
    def wallet_list():
        with db.get_conn() as conn:
            wallets = db.get_wallets(conn)
        return render_template("wallets.html", wallets=wallets)

    @app.route("/wallets/add", methods=["POST"])
    def wallet_add():
        label = request.form.get("label", "").strip()
        xpub = request.form.get("xpub", "").strip()
        if not label or not xpub:
            flash("Both label and xpub are required.", "error")
            return redirect(url_for("wallet_list"))
        # Basic sanity check — zpub starts with 'zpub'
        if not (xpub.startswith("zpub") or xpub.startswith("xpub")):
            flash("XPub must start with 'zpub' (or 'xpub' for some wallets).", "error")
            return redirect(url_for("wallet_list"))
        try:
            with db.get_conn() as conn:
                db.add_wallet(conn, label, xpub)
        except Exception as exc:
            flash(f"Could not add wallet: {exc}", "error")
            return redirect(url_for("wallet_list"))
        flash(f"Wallet '{label}' added. Click Sync Now to load transaction history.", "success")
        return redirect(url_for("wallet_list"))

    @app.route("/wallets/<int:wallet_id>/obsolete", methods=["POST"])
    def wallet_set_obsolete(wallet_id):
        """Mark a wallet obsolete or restore it (Manage Wallets page)."""
        obsolete = request.form.get("obsolete") == "1"
        with db.get_conn() as conn:
            w = db.get_wallet(conn, wallet_id)
            if w:
                db.set_wallet_obsolete(conn, wallet_id, obsolete)
                if obsolete:
                    flash(f"Wallet '{w['label']}' marked obsolete. It's hidden on the "
                          "dashboard but still counts in your cost basis.", "success")
                else:
                    flash(f"Wallet '{w['label']}' restored.", "success")
        return redirect(url_for("wallet_list"))

    @app.route("/settings/show-obsolete", methods=["POST"])
    def set_show_obsolete():
        """Show or hide obsolete wallets on the dashboard (remembered)."""
        show = request.form.get("show") == "1"
        with db.get_conn() as conn:
            db.set_setting(conn, "show_obsolete", "1" if show else "0")
        return redirect(url_for("dashboard") + "#wallets")

    @app.route("/wallets/<int:wallet_id>/delete", methods=["POST"])
    def wallet_delete(wallet_id):
        with db.get_conn() as conn:
            w = db.get_wallet(conn, wallet_id)
            if w:
                db.delete_wallet(conn, wallet_id)
                flash(f"Wallet '{w['label']}' removed.", "success")
        return redirect(url_for("wallet_list"))

    def _build_tx_rows(raw_txs, currency):
        """Build display-ready transaction rows from raw DB rows."""
        rows = []
        for tx in raw_txs:
            if tx["block_time"]:
                date_str = datetime.datetime.utcfromtimestamp(
                    tx["block_time"]
                ).strftime("%Y-%m-%d")
            else:
                date_str = None

            price_at_time = (
                tx["cad_price"] if currency == "cad" else tx["usd_price"]
            )
            fiat_at_time = (
                round(abs(tx["value_sats"]) / 1e8 * price_at_time, 2)
                if price_at_time else None
            )

            if tx["is_transfer"]:
                tx_type = "Transfer"
            elif tx["value_sats"] > 0:
                tx_type = "Received"
            else:
                tx_type = "Sent"

            rows.append({
                "txid": tx["txid"],
                "txid_short": tx["txid"][:16] + "…",
                "date": date_str,
                "block_height": tx["block_height"],
                "btc_amount": round(abs(tx["value_sats"]) / 1e8, 8),
                "value_sats": tx["value_sats"],
                "price_at_time": price_at_time,
                "fiat_at_time": fiat_at_time,
                "tx_type": tx_type,
                "address": tx["address"],
            })
        return rows

    @app.route("/wallets/<int:wallet_id>/detail")
    def wallet_detail(wallet_id):
        """Show full transaction history for a single wallet."""
        with db.get_conn() as conn:
            wallet = db.get_wallet(conn, wallet_id)
            if not wallet:
                flash("Wallet not found.", "error")
                return redirect(url_for("dashboard"))
            currency = db.get_setting(conn, "currency", "usd")
            currency_label = "CAD" if currency == "cad" else "USD"
            latest_price = db.get_latest_price(conn, currency)
            bal_sats = db.get_current_balance_sats(conn, wallet_id)
            avg_price = db.get_avg_purchase_price(conn, wallet_id, currency)
            raw_txs = db.get_wallet_transactions(conn, wallet_id)

        rows = _build_tx_rows(raw_txs, currency)

        return render_template(
            "wallet_detail.html",
            wallet=wallet,
            txs=rows,
            currency=currency,
            currency_label=currency_label,
            latest_price=latest_price,
            balance_btc=round(bal_sats / 1e8, 8),
            balance_fiat=(
                round(bal_sats / 1e8 * latest_price, 2) if latest_price else None
            ),
            avg_price=avg_price,
        )

    @app.route("/wallets/<int:wallet_id>/export.csv")
    def wallet_export_csv(wallet_id):
        """Download wallet transactions as a CSV file."""
        with db.get_conn() as conn:
            wallet = db.get_wallet(conn, wallet_id)
            if not wallet:
                flash("Wallet not found.", "error")
                return redirect(url_for("dashboard"))
            currency = db.get_setting(conn, "currency", "usd")
            currency_label = "CAD" if currency == "cad" else "USD"
            raw_txs = db.get_wallet_transactions(conn, wallet_id)

        rows = _build_tx_rows(raw_txs, currency)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Date", "Type", "BTC Amount",
            f"Price at Time ({currency_label})",
            f"Fiat Value ({currency_label})",
            "Transaction ID",
        ])
        for r in rows:
            writer.writerow([
                r["date"] or "Pending",
                r["tx_type"],
                r["btc_amount"],
                r["price_at_time"] or "",
                r["fiat_at_time"] or "",
                r["txid"],
            ])

        safe_label = wallet["label"].replace(" ", "_")
        filename = f"{safe_label}_transactions.csv"

        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/settings/currency", methods=["POST"])
    def set_currency():
        """Toggle the display currency between USD and CAD."""
        currency = request.form.get("currency", "usd").lower()
        if currency not in ("usd", "cad"):
            currency = "usd"
        with db.get_conn() as conn:
            db.set_setting(conn, "currency", currency)
        return redirect(url_for("dashboard"))

    def _wants_json():
        return (
            request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json"
        )

    @app.route("/sync", methods=["POST"])
    def sync_start():
        """Start a full sync in the background (or attach to a running one)."""
        started = sync.start(source="manual")
        if _wants_json():
            return jsonify(started=started, status=sync.status())
        # No-JavaScript fallback
        if started:
            flash("Sync started in the background. Refresh in a minute to see updated data.", "info")
        else:
            flash("A sync is already running.", "info")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/sync/status")
    def sync_status():
        """Progress of the current (or last) sync, polled by the progress bar."""
        resp = jsonify(sync.status())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/chart-data")
    def api_chart_data():
        """JSON endpoint for the portfolio chart."""
        with db.get_conn() as conn:
            currency = db.get_setting(conn, "currency", "usd")
            data = db.get_portfolio_chart_data(conn, currency)
        return jsonify(data)

    @app.teardown_appcontext
    def _shutdown_scheduler(exc=None):
        pass  # Scheduler is a daemon; it shuts down with the process.

    return app


if __name__ == "__main__":
    # Local development only — gunicorn is used in production
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
