/*
 * sync.js — Sync Now button with a live progress bar.
 *
 * Clicking Sync Now starts a background sync on the server, then polls
 * /sync/status and fills the progress bar. When the sync finishes the page
 * reloads itself so the new balances show up, and the result (success or
 * the error) is shown at the top of the reloaded page.
 *
 * If a sync is already running when a page loads (the 02:00 UTC daily job,
 * or one started from another tab) the bar picks it up automatically.
 */
(function () {
  "use strict";

  var POLL_MS = 800;
  var RESULT_KEY = "cst-sync-result";

  var form = document.getElementById("sync-form");
  var btn = document.getElementById("sync-btn");
  var panel = document.getElementById("sync-panel");
  var msgEl = document.getElementById("sync-message");
  var pctEl = document.getElementById("sync-percent");
  var fill = document.getElementById("sync-fill");
  var resultEl = document.getElementById("sync-result");
  if (!form || !panel) return;

  var statusUrl = panel.getAttribute("data-status-url");
  var polling = false;
  var failures = 0;

  // ---- Result from a sync that just finished (stored before reload) ----
  function showStoredResult() {
    var raw = null;
    try {
      raw = sessionStorage.getItem(RESULT_KEY);
      sessionStorage.removeItem(RESULT_KEY);
    } catch (e) { /* storage unavailable */ }
    if (!raw || !resultEl) return;
    var r;
    try { r = JSON.parse(raw); } catch (e) { return; }
    var div = document.createElement("div");
    div.className = "alert " + (r.error ? "alert-error" : "alert-success");
    div.textContent = r.error ? r.message + " " + r.error : r.message;
    resultEl.appendChild(div);
    if (!r.error) {
      setTimeout(function () {
        div.classList.add("alert-fade");
        setTimeout(function () { div.remove(); }, 600);
      }, 5000);
    }
  }

  // ---- Rendering ----
  function render(s) {
    var pct = Math.max(0, Math.min(100, s.percent || 0));
    fill.style.width = pct + "%";
    pctEl.textContent = Math.floor(pct) + "%";
    if (s.message) msgEl.textContent = s.message;
  }

  function setBusy(busy) {
    panel.hidden = !busy;
    btn.disabled = busy;
    btn.textContent = busy ? "Syncing…" : "Sync Now";
  }

  function finish(s) {
    render({ percent: 100, message: s.error ? s.message : "Sync complete — refreshing…" });
    panel.classList.toggle("sync-panel--error", !!s.error);
    try {
      sessionStorage.setItem(RESULT_KEY, JSON.stringify({
        message: s.message || "Sync complete.",
        error: s.error || null,
      }));
    } catch (e) { /* storage unavailable */ }
    setTimeout(function () { window.location.reload(); }, s.error ? 1500 : 700);
  }

  // ---- Polling ----
  function poll() {
    fetch(statusUrl, { cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (s) {
        failures = 0;
        if (s.running) {
          render(s);
          setTimeout(poll, POLL_MS);
        } else {
          polling = false;
          finish(s);
        }
      })
      .catch(function () {
        failures += 1;
        if (failures >= 5) {
          msgEl.textContent = "Lost contact with the tracker — still trying…";
        }
        setTimeout(poll, Math.min(POLL_MS * failures, 5000));
      });
  }

  function watch(s) {
    setBusy(true);
    panel.classList.remove("sync-panel--error");
    if (s) render(s);
    if (!polling) {
      polling = true;
      setTimeout(poll, POLL_MS);
    }
  }

  // ---- Sync Now button ----
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (polling) return;
    watch({ percent: 0, message: "Starting sync…" });
    fetch(form.action, {
      method: "POST",
      headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) { render(data.status); })
      .catch(function () {
        // Fall back to the plain form post
        polling = false;
        form.submit();
      });
  });

  // ---- On page load ----
  showStoredResult();
  fetch(statusUrl, { cache: "no-store", headers: { "Accept": "application/json" } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (s) { if (s && s.running) watch(s); })
    .catch(function () { /* ignore */ });
})();
