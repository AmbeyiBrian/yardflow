/**
 * Attach to the Chrome window that is open for manual testing.
 *
 * The browser runs on its own with `--remote-debugging-port=9222`, so it outlives
 * any one script: you drive it by hand, and this connects to the *same* tab to
 * look at what you are looking at, or to navigate it somewhere.
 *
 *   node scripts/peek.mjs                     # screenshot + recent API calls
 *   node scripts/peek.mjs /reports            # navigate there first
 *   node scripts/peek.mjs - shot.png          # screenshot to a given path
 *
 * A separate profile from the everyday browser, so nothing here touches real
 * cookies, and none of the cached redirects that caused yesterday's loop exist.
 */
import { chromium } from '@playwright/test'

const goTo = process.argv[2] && process.argv[2] !== '-' ? process.argv[2] : null
const shot = process.argv[3] ?? 'peek.png'

const browser = await chromium.connectOverCDP('http://127.0.0.1:9222')
const context = browser.contexts()[0]
const pages = context.pages()
const page = pages.find((p) => p.url().includes('localhost:5173')) ?? pages[0]

if (!page) {
  console.log('no page open in that browser')
  process.exit(1)
}

const calls = []
page.on('response', (r) => {
  const u = new URL(r.url())
  if (u.pathname.startsWith('/api')) calls.push(`${r.status()} ${u.pathname}${u.search}`)
})

if (goTo) {
  await page.goto(`http://demo.localhost:5173${goTo}`)
  await page.waitForLoadState('networkidle').catch(() => {})
}
await page.waitForTimeout(1200)

console.log('url:  ', page.url())
console.log('title:', await page.title())

const alerts = (await page.getByRole('alert').allInnerTexts().catch(() => []))
  .map((s) => s.trim())
  .filter(Boolean)
if (alerts.length) console.log('alerts:', JSON.stringify(alerts))

const rows = await page.locator('main tbody tr, main li').count().catch(() => 0)
console.log('rows: ', rows)

if (calls.length) {
  console.log('api calls seen while attached:')
  for (const c of calls) console.log('  ' + c)
}

await page.screenshot({ path: shot, fullPage: false })
console.log('shot: ', shot)

// Leave the browser running — it belongs to the person testing, not to this script.
await browser.close()
