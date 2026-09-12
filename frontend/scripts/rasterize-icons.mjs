/**
 * Rasterise the app icons from public/favicon.svg and public/icon-maskable.svg.
 *
 * The SVG favicon covers every modern browser tab, but two places still refuse
 * vector: iOS wants a PNG for `apple-touch-icon`, and Android's install prompt
 * wants PNGs in the manifest. Rather than hand-maintain five bitmaps that drift
 * out of step with the mark, generate them from the same two SVGs.
 *
 *   node scripts/rasterize-icons.mjs
 *
 * Run it when the mark changes. The outputs are committed, so a normal build and
 * a normal checkout need neither Playwright nor this script.
 */

import { chromium } from '@playwright/test'
import { mkdir, readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const publicDir = resolve(here, '..', 'public')

/** [source svg, output png, pixel size] */
const TARGETS = [
  ['favicon.svg', 'apple-touch-icon.png', 180],
  ['favicon.svg', 'pwa-192x192.png', 192],
  ['favicon.svg', 'pwa-512x512.png', 512],
  ['icon-maskable.svg', 'pwa-maskable-512x512.png', 512],
]

const browser = await chromium.launch()
try {
  for (const [source, output, size] of TARGETS) {
    const svg = await readFile(resolve(publicDir, source), 'utf8')
    const page = await browser.newPage({ viewport: { width: size, height: size } })
    // A page with no margin and the SVG stretched to fill it, so the screenshot
    // is the icon exactly — no padding to trim afterwards.
    await page.setContent(
      `<style>html,body{margin:0;padding:0}svg{display:block;width:${size}px;height:${size}px}</style>${svg}`,
      { waitUntil: 'load' },
    )
    await mkdir(publicDir, { recursive: true })
    await page.screenshot({ path: resolve(publicDir, output), omitBackground: true })
    await page.close()
    console.log(`${output.padEnd(26)} ${size}x${size} from ${source}`)
  }
} finally {
  await browser.close()
}
