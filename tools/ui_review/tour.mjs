// Screenshot tour for the ui-review-loop skill. Walks every reviewer-visible
// state of the statementledger web UI against a running server (BASE_URL)
// and writes labeled PNGs to SHOTS_DIR. The tour is the evaluation function:
// same states, same order, same framing every round.
//
//   node tools/ui_review/tour.mjs --phase=empty   # 01 only, before seeding
//   node tools/ui_review/tour.mjs --phase=main    # everything else
//
// The "main" phase drives real interactions (upload, duplicate re-upload,
// re-probe, dialogs) against the sacrificial account, resetting it first so
// every round captures identical states.

import { readFileSync } from "node:fs";
import { chromium } from "playwright-core";

const BASE = process.env.BASE_URL || "http://127.0.0.1:8765";
const SHOTS = process.env.SHOTS_DIR || "/tmp/ui-shots";
const CHROME = (process.env.CHROME_EXECUTABLE ||
  `${process.env.HOME}/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome`);
const phase = process.argv.find((arg) => arg.startsWith("--phase="))?.slice(8) || "main";
const manifest = phase === "main"
  ? JSON.parse(readFileSync("data/review-manifest.json", "utf8"))
  : null;

const browser = await chromium.launch({ executablePath: CHROME });
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  reducedMotion: "reduce",
});
async function fresh() {
  const p = await context.newPage();
  p.on("pageerror", (error) => console.error("PAGE ERROR:", error.message));
  return p;
}

async function settle(p, ms = 400) {
  await p.waitForLoadState("networkidle");
  await p.waitForTimeout(ms); // htmx swaps, SSE replay, font settle
}

async function shot(p, label, { fullPage = true } = {}) {
  await settle(p);
  await p.screenshot({ path: `${SHOTS}/${label}.png`, fullPage });
  console.log("shot", label);
}

const api = context.request;

// --- phase: empty (before seeding) --------------------------------------------
if (phase === "empty") {
  const p = await fresh();
  await p.goto(`${BASE}/`);
  await shot(p, "01-accounts-empty");
}

// --- phase: main ---------------------------------------------------------------
if (phase === "main") {
  const A = manifest.accounts;

  const p = await fresh();
  await p.goto(`${BASE}/`);
  await shot(p, "02-accounts-list");

  await p.goto(`${BASE}/accounts/${A.parsed}`);
  await shot(p, "03-account-parsed");

  await p.locator("details.pin-details").click();
  await shot(p, "04-account-pin-picker", { fullPage: false });

  await p.goto(`${BASE}/accounts/${A.parsed}/statements/${manifest.parsedSid}`);
  await shot(p, "05-statement-parsed");

  // the durable failure state (§6.3c): banner + attempts + CLI + re-probe
  const f = await fresh();
  await f.goto(`${BASE}/accounts/${A.failed}/statements/${manifest.failedSid}`);
  await shot(f, "06-statement-failed");

  await f.locator("details.launch-dialog").click();
  await shot(f, "07-launch-dialog", { fullPage: false });

  // reset the sacrificial account, then drive real upload outcomes
  await api.delete(`${BASE}/accounts/${A.sacrificial}`).catch(() => {});
  await api.post(`${BASE}/accounts`, { form: { display_name: "Probe Playground", registry_key: "" } });
  const s = await fresh();
  await s.goto(`${BASE}/accounts/${A.sacrificial}`);
  await s.locator('input[type="file"]').setInputFiles("tests/fixtures/copperkettle_checking.pdf");
  await s.locator("form.upload-zone button").click();
  await s.waitForSelector(".banner-ok", { timeout: 30000 });
  await shot(s, "09-upload-outcome-parsed");

  await s.locator('input[type="file"]').setInputFiles("tests/fixtures/copperkettle_checking.pdf");
  await s.locator("form.upload-zone button").click();
  await s.waitForSelector(".banner-warn", { timeout: 30000 });
  await shot(s, "10-upload-duplicate");

  await p.goto(`${BASE}/registry`);
  await shot(p, "11-registry");

  await p.goto(`${BASE}/runs`);
  await shot(p, "12-runs-list");

  // run views: wait for the SSE replay to populate the timeline
  const r = await fresh();
  r.setViewportSize({ width: 1440, height: 1000 });
  await r.goto(`${BASE}/runs/${manifest.runConverged}`);
  await r.waitForFunction(() => document.querySelectorAll("#timeline .tl-round").length > 0, null, { timeout: 30000 });
  await r.waitForTimeout(600); // code pane fetch
  await shot(r, "13-run-view-converged", { fullPage: false });

  await r.goto(`${BASE}/runs/${manifest.runFailed}`);
  await r.waitForFunction(() => document.querySelectorAll("#timeline .tl-round").length > 0, null, { timeout: 30000 });
  await r.waitForFunction(() => document.querySelectorAll("#overlay .overlay-band").length > 0, null, { timeout: 30000 });
  await r.waitForTimeout(600);
  await shot(r, "14-run-view-failed-overlay", { fullPage: false });

  // failure card on the failed ACCOUNT page: attempts + launch dialog + probe
  const a2 = await fresh();
  await a2.goto(`${BASE}/accounts/${A.failed}`);
  await shot(a2, "15-account-failed-statement");
}

await browser.close();
console.log("tour complete:", phase);
