// DOM verification for review finding 11: numeric table headers must be
// right-aligned over right-aligned cells.
import { chromium } from "playwright-core"

const browser = await chromium.launch({
  executablePath: `${process.env.HOME}/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome`,
})
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
await page.goto("http://127.0.0.1:8799/app/accounts/blue-lantern-savings", { waitUntil: "networkidle" })
await page.waitForTimeout(1000)
const aligns = await page.evaluate(() =>
  [...document.querySelectorAll("th")].map((th) => ({
    text: th.textContent,
    textAlign: getComputedStyle(th).textAlign,
  })),
)
console.log(JSON.stringify(aligns))
// review round-2 finding 8: does a statement filename link render underlined?
const deco = await page.evaluate(() => {
  const a = document.querySelector('td a[href*="/statements/"]')
  return a ? { text: a.textContent, textDecoration: getComputedStyle(a).textDecorationLine } : null
})
console.log("filename link:", JSON.stringify(deco))
await browser.close()
