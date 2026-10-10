// Targeted checks for the run-view fixes: resizable sidebar, header/badge
// overflow, converged-banner copy. Launches a canned run via the API, then
// drags the separator and measures.
import { chromium } from "playwright-core";

const BASE = "http://127.0.0.1:8799"
const FIX = "/home/henryhuang/Documents/Code/balance-analysis/.claude/worktrees/web-ui/tests/fixtures"
const CHROME = `${process.env.HOME}/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome`

const browser = await chromium.launch({ executablePath: CHROME })
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
const errors = []
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`))
page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text()}`) })

// seed: account with a failed statement (copper pin + blue pdf), then launch
const slug = "resize-check-bank-with-a-rather-long-display-name" // slugify(name)
const made = await fetch(`${BASE}/accounts`, {
  method: "POST",
  headers: { accept: "application/json", "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ display_name: "Resize Check Bank With A Rather Long Display Name", registry_key: "us__copperkettle__checking__1" }),
})
if (!made.ok && made.status !== 409) throw new Error(`create account: ${made.status} ${await made.text()}`) // 409 = leftover from a previous check run
const fs = await import("node:fs/promises")
const pdf = await fs.readFile(`${FIX}/bluelantern_savings.pdf`)
const form = new FormData()
form.append("file", new Blob([pdf], { type: "application/pdf" }), "bluelantern_savings.pdf")
const up = await fetch(`${BASE}/accounts/${slug}/statements`, { method: "POST", headers: { accept: "application/json" }, body: form })
if (up.status !== 201 && up.status !== 200 && up.status !== 409) throw new Error(`upload: ${up.status} ${await up.text()}`)
const metas = await (await fetch(`${BASE}/accounts/${slug}`, { headers: { accept: "application/json" } })).json()
const { run_id } = await (await fetch(`${BASE}/runs`, {
  method: "POST",
  headers: { accept: "application/json", "content-type": "application/json" },
  body: JSON.stringify({ account_slug: slug, statement_ids: metas.metas.map((m) => m.id), mode: "draft", rounds: 500, max_probes: 500 }),
})).json()
console.log("run:", run_id)

await page.goto(`${BASE}/runs/${run_id}`, { waitUntil: "networkidle" })
await page.waitForTimeout(4000)

// 1. resize: drag the separator 150px left (sidebar grows)
const aside = page.locator("aside")
const before = await aside.boundingBox()
const handle = page.getByRole("separator", { name: /resize/i })
const hb = await handle.boundingBox()
await page.mouse.move(hb.x + hb.width / 2, hb.y + 100)
await page.mouse.down()
await page.mouse.move(hb.x - 150, hb.y + 100, { steps: 8 })
await page.mouse.up()
const after = await aside.boundingBox()
const stored = await page.evaluate(() => localStorage.getItem("run-sidebar-w"))
console.log("resize:", JSON.stringify({ before: before.width, after: after.width, grew: after.width - before.width, stored }))

// 2. no horizontal overflow anywhere (long account name + model + badges)
const overflow = await page.evaluate(() => {
  const de = document.documentElement
  const sidebar = document.querySelector("aside")
  return {
    pageOverflowX: de.scrollWidth - de.clientWidth,
    sidebarOverflowX: sidebar ? sidebar.scrollWidth - sidebar.clientWidth : null,
  }
})
console.log("overflow:", JSON.stringify(overflow))

// 3. reload — persisted width sticks
await page.reload({ waitUntil: "networkidle" })
await page.waitForTimeout(1500)
const persisted = await (await aside.boundingBox()).width
console.log("persisted width:", persisted)

await page.screenshot({ path: "/home/henryhuang/.claude/jobs/25fa58b5/tmp/spa-shots/13-run-resized.png" })

// 4. cancel → banner copy (no "Parser ?")
await page.getByRole("button", { name: "Cancel", exact: true }).click()
await page.waitForTimeout(2500)
const banner = await page.evaluate(() => {
  const cards = [...document.querySelectorAll("div")]
  return cards.map((c) => c.textContent).find((t) => t?.includes("Cancelled")) ?? "(no banner found)"
})
console.log("banner:", banner.slice(0, 80))
console.log(errors.length ? `ERRORS:\n${errors.join("\n")}` : "no console errors")
await browser.close()
process.exit(errors.length ? 1 : 0)
