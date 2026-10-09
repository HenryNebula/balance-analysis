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
  var roundOutput = {};   // round -> live stdout tail from the round_ran event
  var viewingOutput = false; // code pane currently shows a trace, not code
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
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-zoom]"), function (button) {
        button.classList.toggle(
          "active", parseFloat(button.getAttribute("data-zoom")) === zoom);
      });
  }

  // fit the page to the pane on load — clipped amounts are unreadable, and
  // the amounts are exactly what the gate verifies
  function fitToPane() {
    if (!pageImg.naturalWidth) { return; }
    var viewport = document.querySelector(".pdf-viewport");
    zoom = Math.min(1, (viewport.clientWidth - 24) / pageImg.naturalWidth);
    applyZoom();
  }
  pageImg.addEventListener("load", fitToPane);
  pageImg.complete && fitToPane();

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
    band.title = "what the probe looked at: " +
      (selection.label || selection.line_ids.length + " lines");
    band.style.left = rect[0] * SCALE + "px";
    band.style.top = rect[1] * SCALE + "px";
    band.style.width = (rect[2] - rect[0]) * SCALE + "px";
    band.style.height = (rect[3] - rect[1]) * SCALE + "px";
    var tag = document.createElement("span");
    tag.className = "overlay-tag";
    tag.style.fontSize = Math.round(11 / zoom) + "px"; // legible at any zoom
    tag.textContent = selection.label
      ? String(selection.label).slice(0, 44) : "selection";
    band.appendChild(tag);
    overlay.appendChild(band);
    selectionLabel.textContent = "selection: r" + selection.round + " · " +
      (selection.label || (selection.line_ids.length + " lines"));
  }

  // selectRound: mark + refresh panes. scrub: the MANUAL version, which
  // additionally unpins auto-follow. Auto-follow must call selectRound —
  // calling scrub from follow unpinned itself after the first round.
  function selectRound(round) {
    scrubbedRound = round;
    document.querySelectorAll(".tl-round").forEach(function (row) {
      row.classList.toggle(
        "selected", Number(row.getAttribute("data-round")) === round);
    });
    renderOverlay(lastSelection[round]);
    loadCode(round);
  }

  function scrub(round) {
    follow = false;
    followDot.classList.remove("active");
    selectRound(round);
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
    if (follow) { timeline.scrollTop = timeline.scrollHeight; } // keep pace
    return row;
  }

  function addBadge(number, html, cls) {
    var entry = rounds[number];
    if (!entry) { return; }
    var badge = document.createElement("span");
    badge.className = "chip " + (cls || "chip-muted");
    badge.innerHTML = html;
    entry.detail.appendChild(badge);
    if (follow) { timeline.scrollTop = timeline.scrollHeight; }
  }

  function clearRunning(number) {
    var entry = rounds[number];
    if (!entry) { return; }
    entry.detail.querySelectorAll(".tl-running")
     .forEach(function (badge) { badge.remove(); });
  }

  function addTraceButton(number) {
    var entry = rounds[number];
    if (!entry || entry.traceButton) { return; }
    var trace = document.createElement("button");
    trace.className = "btn btn-small tl-trace";
    trace.textContent = "trace ▸";
    trace.title = "what this round printed";
    trace.addEventListener("click", function () {
      scrub(number);      // moves selection + overlay (starts a code fetch)
      loadOutput(number); // then takes the pane; the code fetch checks the
                          // flag and won't clobber a trace being viewed
    });
    entry.detail.appendChild(trace);
    entry.traceButton = trace;
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
        addBadge(number, "running…", "chip-muted tl-running");
        if (follow) {
          selectRound(number);
          timeline.scrollTop = timeline.scrollHeight;
        }
        break;
      case "round_ran":
        roundRow(number);
        clearRunning(number);
        addBadge(number, event.returncode === 0 ? "ran ✓" : "ran ✗",
                 event.returncode === 0 ? "chip-ok" : "chip-fail");
        if (event.stdout_tail) {
          roundOutput[number] = event.stdout_tail;
          addTraceButton(number);
          if (scrubbedRound === number && viewingOutput) { loadOutput(number); }
        }
        if (follow || scrubbedRound === number) { loadCode(number); }
        break;
      case "gate":
        roundRow(number);
        clearRunning(number);
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
        clearRunning(number);
        addBadge(number, "lint ✗", "chip-fail");
        addNote(number, String(event.violation || "").slice(0, 120));
        break;
      case "chat_failed":
        roundRow(number);
        clearRunning(number);
        addBadge(number, "chat ✗", "chip-fail");
        addNote(number, String(event.error || "").slice(0, 120));
        break;
      case "feedback_sent":
        roundRow(number);
        addTraceButton(number);
        addNote(number, String(event.summary || "").slice(0, 140));
        break;
      case "converged":
        setStatus("converged ✓ " + (event.key || ""), "chip-ok");
        finish("Converged — parser " + (event.key || "") +
               (event.saved ? " saved to the registry" : ""));
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
        setStatus(event.status, statusClass(event.status));
        finish("Run " + event.status + ".");
        break;
    }
  }

  function setStatus(text, cls) {
    statusChip.textContent = text;
    statusChip.className = "chip " + (cls || "chip-key");
  }

  function statusClass(status) {  // semantic color, same map as the server
    return {converged: "chip-ok", failed: "chip-fail",
            cancelled: "chip-muted", orphaned: "chip-warn",
            cancelling: "chip-warn", running: "chip-key"}[status]
           || "chip-key";
  }

  function finish(message) {
    if (terminal) { return; }
    terminal = true;
    cancelButton.disabled = true;
    cancelButton.style.display = "none";
    var banner = document.createElement("div");
    banner.className = "banner banner-warn";
    banner.textContent = message;
    timeline.appendChild(banner);
    timeline.scrollTop = timeline.scrollHeight;
    // converged runs end in an action, not a shrug — and NEVER alongside a
    // contradictory "(not saved)" banner: the fetch replaces it wholesale
    fetch("/runs/" + config.id, {headers: {accept: "application/json"}})
      .then(function (response) { return response.json(); })
      .then(function (run) {
        if (run.status !== "converged") { return; }
        var detail = document.createElement("div");
        detail.className = "banner banner-ok";
        var saved = "Parser " + (run.converged_key || "?") +
          (run.auto_parse === null
            ? " verified but not registered (attach or canned run)."
            : " saved to the registry.");
        detail.textContent = saved + " ";
        if (run.auto_parse && run.auto_parse.length) {
          var parsed = run.auto_parse.filter(function (entry) {
            return entry.status === "parsed"; }).length;
          detail.textContent += parsed + " of " + run.auto_parse.length +
            " pending statement(s) parsed. ";
        }
        var link = document.createElement("a");
        link.className = "btn btn-small";
        link.href = "/accounts/" + config.slug;
        link.textContent = "Go to account";
        detail.appendChild(link);
        banner.remove();
        timeline.appendChild(detail);
        timeline.scrollTop = timeline.scrollHeight;
      })
      .catch(function () { /* the plain banner already says the essentials */ });
  }

  // -- code pane -----------------------------------------------------------

  function loadCode(round, retry) {
    viewingOutput = false;
    fetch("/runs/" + config.id + "/rounds/" + round)
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (detail) {
        if (viewingOutput) { return; } // a trace took the pane meanwhile
        if (!detail) {
          // the round_saved event can beat the manager's workdir resolution —
          // the file exists but the run record doesn't know where yet
          if (!retry) { setTimeout(function () { loadCode(round, true); }, 900); }
          return;
        }
        codeTitle.textContent = "round_" + round + ".py · " + detail.kind;
        codeEl.textContent = detail.code;
        codeEl.classList.remove("code-fresh");
        void codeEl.offsetWidth; // restart the animation
        codeEl.classList.add("code-fresh");
      });
  }

  function loadOutput(round) {
    viewingOutput = true;
    // the live event tail needs no workdir round-trip — it arrives with the
    // round, exactly when the overlay does; the file is the fuller version
    if (roundOutput[round] !== undefined) {
      codeTitle.textContent = "round_" + round + ".out · live tail";
      codeEl.textContent = roundOutput[round];
      return;
    }
    fetch("/runs/" + config.id + "/rounds/" + round + "/output")
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (detail) {
        codeTitle.textContent = "round_" + round + ".out";
        codeEl.textContent = detail
          ? detail.output : "(no captured output for round " + round + ")";
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
