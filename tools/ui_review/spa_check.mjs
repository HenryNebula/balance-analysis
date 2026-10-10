// SPA smoke tour (web/frontend at /app): walks the React UI end to end —
// accounts, upload + parse, statement view, failed-state launch dialog, the
// live run view (SSE timeline + overlay + trace), cancel — screenshotting
// each settled state and failing on console errors. Run with the dev server
// on BASE (default http://127.0.0.1:8799).
//
//   node tools/ui_review/spa_check.mjs [base]
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.argv[2] || "http://127.0.0.1:8799";
const SHOTS = process.env.SHOTS_DIR || "/home/henryhuang/.claude/jobs/25fa58b5/tmp/spa-shots";
const FIX = new URL("../../tests/fixtures/", import.meta.url).pathname;
const CHROME = process.env.CHROME ||
  `${process.env.HOME}/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome`;

mkdirSync(SHOTS, { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

const errors = [];
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") errors.push(`console: ${m.text()}`);
});

async function shot(name, opts) {
  await sleep(700); // settle animations/transitions before the frame
  await page.screenshot({ path: `${SHOTS}/${name}.png`, ...opts });
  console.log(`shot ${name}`);
}

// -- accounts (empty) ---------------------------------------------------------
await page.goto(`${BASE}/app`, { waitUntil: "networkidle" });
await page.getByRole("button", { name: "New account" }).waitFor();
await shot("01-accounts-empty");

// -- create account ------------------------------------------------------------
await page.getByRole("button", { name: "New account" }).click();
await page.getByLabel(/Account name/).fill("Blue Lantern Savings");
await page.getByRole("button", { name: "Create", exact: true }).click();
await page.getByText("Drop a statement PDF", { exact: false }).waitFor();
await shot("02-account-empty");

// -- upload + parse ------------------------------------------------------------
await page.setInputFiles('input[type="file"]', `${FIX}/bluelantern_savings.pdf`);
await page.getByText("Parsed", { exact: false }).first().waitFor({ timeout: 60_000 });
await shot("03-account-parsed");

// -- statement view --------------------------------------------------------------
await page.getByRole("link", { name: /bluelantern_savings\.pdf/ }).click();
await page.getByText("RECONCILED", { exact: false }).waitFor();
await shot("04-statement", { fullPage: false });

// -- failed account (wrong pin) → calm no-parser card -----------------------------
await page.getByRole("link", { name: "statementledger" }).click();
await page.getByRole("button", { name: "New account" }).click();
await page.getByLabel(/Account name/).fill("Fail Bank");
await page.getByText("detection mode (no pin)").first().click();
await page.getByRole("option", { name: /copperkettle/ }).click();
await page.getByRole("button", { name: "Create", exact: true }).click();
await page.getByText("Drop a statement PDF", { exact: false }).waitFor();
await page.setInputFiles('input[type="file"]', `${FIX}/bluelantern_savings.pdf`);
await page.getByText("did not verify", { exact: false }).waitFor({ timeout: 60_000 });
await shot("05-failed-pinned");

// -- detection-mode account → amber "no parser yet" -------------------------------
await page.getByRole("link", { name: "statementledger" }).click();
await page.getByRole("button", { name: "New account" }).click();
await page.getByLabel(/Account name/).fill("Mystery Bank");
await page.getByRole("button", { name: "Create", exact: true }).click();
await page.getByText("Drop a statement PDF", { exact: false }).waitFor();
await page.setInputFiles('input[type="file"]', `${FIX}/copperkettle_checking.pdf`);
await page.getByText("Parsed", { exact: false }).first().waitFor({ timeout: 60_000 }); // detection: copper verifies
await shot("06-mystery-parsed");

// -- launch a distill run (canned slow probe) → live run view ----------------------
// force a failed statement for the launch button: new account with blue pdf + copper pin
await page.getByRole("link", { name: "statementledger" }).click();
await page.getByRole("button", { name: "New account" }).click();
await page.getByLabel(/Account name/).fill("Distill Me");
await page.getByText("detection mode (no pin)").first().click();
await page.getByRole("option", { name: /copperkettle/ }).click();
await page.getByRole("button", { name: "Create", exact: true }).click();
await page.getByText("Drop a statement PDF", { exact: false }).waitFor();
await page.setInputFiles('input[type="file"]', `${FIX}/bluelantern_savings.pdf`);
await page.getByText("did not verify", { exact: false }).waitFor({ timeout: 60_000 });

await page.getByRole("button", { name: /Register bank|Distill update/ }).first().click();
await page.getByLabel(/Rounds/).fill("500");
await page.getByLabel(/Max probes/).fill("500");
await page.getByRole("button", { name: "Launch run" }).click();
await page.getByText("Trajectory").waitFor();

// wait for ≥2 live rounds + a selection on the PDF
await page.locator("li:has-text('r1')").waitFor({ timeout: 60_000 });
await page.locator(".absolute.rounded-\\[2px\\].border-2").waitFor({ timeout: 60_000 });
const live = await page.evaluate(() => ({
  status: document.querySelector("[data-slot='badge']")?.textContent,
  rounds: [...document.querySelectorAll("ol li button")].map((b) => b.textContent),
  selection: document.body.textContent.includes("selection: r"),
}));
console.log("live state:", JSON.stringify(live));
await shot("07-run-live");

// -- trace pane (the newest round's trace is the live one) -------------------------
await page.getByRole("button", { name: /trace ▸/ }).last().click();
await page.getByText(/round_\d+\.out/).waitFor();
await shot("08-run-trace");

// -- scrub an earlier round (unpins follow) -----------------------------------------
await page.locator("ol li button", { hasText: "r0" }).first().click();
await sleep(400);
const followState = await page.evaluate(() => ({
  followBtn: [...document.querySelectorAll("button")].find((b) => b.textContent.includes("follow"))?.textContent,
}));
console.log("after scrub:", JSON.stringify(followState));
await shot("09-run-scrubbed");

// -- cancel -------------------------------------------------------------------------
await page.getByRole("button", { name: "Cancel", exact: true }).click();
await page.getByText("Cancelled.", { exact: false }).waitFor({ timeout: 30_000 });
await shot("10-run-cancelled");

// -- registry + runs list --------------------------------------------------------------
await page.goto(`${BASE}/app/registry`, { waitUntil: "networkidle" });
await shot("11-registry");
await page.goto(`${BASE}/app/runs`, { waitUntil: "networkidle" });
await shot("12-runs");

console.log(errors.length ? `CONSOLE ERRORS:\n${errors.join("\n")}` : "no console errors");
await browser.close();
process.exit(errors.length ? 1 : 0);
