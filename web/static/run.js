/* statementledger run view (docs/web-ui-design.md §9.4) — vanilla JS, no
 * framework: an EventSource over /runs/{id}/events drives the round
 * timeline, the PDF overlay follows the newest probe selection (click any
 * round to scrub; any scrub unpins auto-follow), the code pane fetches
 * round_N.py, and the cancel button DELETEs the run. If SSE dies the page
 * falls back to polling the run status. Overlay coordinates arrive in
 * pdfplumber points and are multiplied by the raster scale (2.0). */
(function () {
  "use strict";

  var config = JSON.parse(document.getElementById("run-config").textContent);
  var SCALE = 2.0;
  var zoom = 0.75;

  var statusChip = document.getElementById("run-status");
  var cancelButton = document.getElementById("cancel-run");
  var timeline = document.getElementById("timeline");
  var overlay = document.getElementById("overlay");
  var stack = document.getElementById("pdf-stack");
  var pageImg = document.getElementById("page-img");
  var pageSelect = document.getElementById("page-select");
  var selectionLabel = document.getElementById("selection-label");
  var followDot = document.getElementById("follow-dot");
  var codeEl = document.getElementById("code");
  var codeTitle = document.getElementById("code-title");

  var rounds = {};        // round -> {el, selections: [], kind}
  var lastSelection = {}; // round -> last probe selection
  var follow = true;
  var currentPage = 0;
  var terminal = false;

  function pagePath(index) {
    return "/accounts/" + config.slug + "/statements/" + config.sid +
           "/pages/" + index + ".png";
  }

  function setPage(index) {
    currentPage = index;
    pageImg.src = pagePath(index);
    renderOverlay(lastSelection[scrubbedRound]);
  }

  function applyZoom() {
    stack.style.transform = "scale(" + zoom + ")";
  }

  // -- overlay -----------------------------------------------------------

  var scrubbedRound = null;

  function renderOverlay(selection) {
    overlay.innerHTML = "";
    if (!selection || selection.page !== currentPage) {
      selectionLabel.textContent =
        selection && selection.page !== currentPage
          ? "selection: round " + selection.round + " is on page " +
            (selection.page + 1) + " — switch pages to see it"
          : "selection: —";
      return;
    }
    var rect = selection.rect;
    var band = document.createElement("div");
    band.className = "overlay-band";
    band.style.left = rect[0] * SCALE + "px";
    band.style.top = rect[1] * SCALE + "px";
    band.style.width = (rect[2] - rect[0]) * SCALE + "px";
    band.style.height = (rect[3] - rect[1]) * SCALE + "px";
    overlay.appendChild(band);
    selectionLabel.textContent = "selection: r" + selection.round + " · " +
      (selection.label || (selection.line_ids.length + " lines"));
  }

  function scrub(round) {
    scrubbedRound = round;
    follow = false;
    followDot.classList.remove("active");
    renderOverlay(lastSelection[round]);
    loadCode(round);
  }

  // -- timeline ------------------------------------------------------------

  function roundRow(number) {
    if (rounds[number]) { return rounds[number].el; }
    var row = document.createElement("div");
    row.className = "tl-round";
    row.setAttribute("data-round", number);
    var head = document.createElement("button");
    head.className = "tl-head";
    head.textContent = "r" + number;
    head.addEventListener("click", function () { scrub(number); });
    row.appendChild(head);
    var detail = document.createElement("div");
    detail.className = "tl-detail";
    row.appendChild(detail);
    timeline.appendChild(row);
    rounds[number] = {el: row, detail: detail};
    return row;
  }

  function addBadge(number, html, cls) {
    var entry = rounds[number];
    if (!entry) { return; }
    var badge = document.createElement("span");
    badge.className = "chip " + (cls || "chip-muted");
    badge.innerHTML = html;
    entry.detail.appendChild(badge);
  }

  function addNote(number, text) {
    var entry = rounds[number];
    if (!entry) { return; }
    var note = document.createElement("div");
    note.className = "tl-note";
    note.textContent = text;
    entry.detail.appendChild(note);
  }

  function gateSummary(verdict) {
    if (!verdict) { return "?"; }
    if (verdict.verified) { return "gate ✓"; }
    var first = (verdict.results || [])[0] || {};
    var error = (first.error || "").split("\n")[0].slice(0, 60);
    return "gate ✗ " + (verdict.clean || 0) + "/" +
           ((verdict.results || []).length || "?") +
           (error ? " · " + error : "");
  }

  // -- events --------------------------------------------------------------

  function handle(event) {
    var number = event.round;
    switch (event.type) {
      case "round_saved":
        roundRow(number);
        addBadge(number, event.kind === "probe" ? "probe" : "script",
                 event.kind === "probe" ? "chip-key" : "chip-muted");
        if (follow) { scrub(number); }
        break;
      case "round_ran":
        roundRow(number);
        addBadge(number, event.returncode === 0 ? "ran ✓" : "ran ✗",
                 event.returncode === 0 ? "chip-ok" : "chip-fail");
        break;
      case "gate":
        roundRow(number);
        var verdict = event.verdict || {};
        addBadge(number, gateSummary(verdict),
                 verdict.verified ? "chip-ok" : "chip-fail");
        break;
      case "probe_select":
        roundRow(number);
        event.rect = event.rect || [];
        lastSelection[number] = event;
        if (follow || scrubbedRound === number) {
          renderOverlay(event);
        }
        break;
      case "lint_rejected":
        roundRow(number);
        addBadge(number, "lint ✗", "chip-fail");
        addNote(number, String(event.violation || "").slice(0, 120));
        break;
      case "chat_failed":
        roundRow(number);
        addBadge(number, "chat ✗", "chip-fail");
        addNote(number, String(event.error || "").slice(0, 120));
        break;
      case "feedback_sent":
        roundRow(number);
        addNote(number, String(event.summary || "").slice(0, 140));
        break;
      case "converged":
        setStatus("converged ✓ " + (event.key || ""), "chip-ok");
        finish("Converged — parser " + (event.key || "") +
               (event.saved ? " saved to the registry" : " (not saved)"));
        break;
      case "failed":
        setStatus("failed ✗", "chip-fail");
        finish("Failed — " + (event.reason || ""));
        break;
      case "cancelled":
        setStatus("cancelled", "chip-muted");
        finish("Cancelled.");
        break;
      case "run_status": // server-side terminal (e.g. SIGKILL): stop here
        setStatus(event.status, "chip-warn");
        finish("Run " + event.status + ".");
        break;
    }
  }

  function setStatus(text, cls) {
    statusChip.textContent = text;
    statusChip.className = "chip " + (cls || "chip-key");
  }

  function finish(message) {
    if (terminal) { return; }
    terminal = true;
    cancelButton.disabled = true;
    var banner = document.createElement("div");
    banner.className = "banner banner-warn";
    banner.textContent = message;
    timeline.appendChild(banner);
    timeline.scrollTop = timeline.scrollHeight;
  }

  // -- code pane -----------------------------------------------------------

  function loadCode(round) {
    fetch("/runs/" + config.id + "/rounds/" + round)
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (detail) {
        if (!detail) {
          codeTitle.textContent = "";
          codeEl.textContent = "(no round_" + round + ".py yet)";
          return;
        }
        codeTitle.textContent = "round_" + round + ".py · " + detail.kind;
        codeEl.textContent = detail.code;
      });
  }

  // -- wiring ---------------------------------------------------------------

  if (pageSelect) {
    pageSelect.addEventListener("change", function () {
      setPage(parseInt(pageSelect.value, 10) || 0);
    });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-zoom]"), function (button) {
      button.addEventListener("click", function () {
        zoom = parseFloat(button.getAttribute("data-zoom"));
        applyZoom();
      });
    });

  followDot.addEventListener("click", function () {
    follow = !follow;
    followDot.classList.toggle("active", follow);
  });

  cancelButton.addEventListener("click", function () {
    fetch("/runs/" + config.id, {method: "DELETE"})
      .then(function () { setStatus("cancelling…", "chip-warn"); })
      .catch(function () { setStatus("cancel failed", "chip-fail"); });
  });

  applyZoom();

  var source = new EventSource("/runs/" + config.id + "/events");
  source.onmessage = function (message) {
    try { handle(JSON.parse(message.data)); } catch (error) { /* skip */ }
  };
  source.addEventListener("run_status", function (message) {
    try { handle({type: "run_status", status: JSON.parse(message.data).status}); }
    catch (error) { /* skip */ }
  });
  source.onerror = function () {
    // degraded mode (§9.4): poll the run record until terminal
    if (terminal) { source.close(); return; }
    var poll = setInterval(function () {
      fetch("/runs/" + config.id, {headers: {accept: "application/json"}})
        .then(function (response) { return response.json(); })
        .then(function (run) {
          setStatus(run.status, "chip-key");
          if (["converged", "failed", "cancelled", "orphaned"]
              .indexOf(run.status) >= 0) {
            clearInterval(poll);
            terminal = true;
            finish("Run " + run.status + " (polling fallback — reconnect " +
                   "for the full trajectory).");
          }
        })
        .catch(function () { /* keep polling */ });
    }, 2000);
  };
})();
