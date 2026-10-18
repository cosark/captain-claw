// Screenshots of the kiosk for its Getting started guide — or, with
// MODE=guide, of the guide itself plus a check of how it behaves. Run by
// run.sh against a throwaway Flight Deck it has set up. Playwright is not a
// Flight Deck dependency: use a global install (NODE_PATH) or point
// PLAYWRIGHT_MODULE at one.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.BASE || 'http://127.0.0.1:25191'
const RUN = process.env.E2E_RUN_DIR || path.join(__dirname, '.run')
const MODE = process.env.MODE || 'assets'
const OUT = path.join(RUN, MODE === 'guide' ? 'guide' : 'png')
const EMAIL = 'maya@example.com', PASSWORD = 'correct-horse-battery'
fs.mkdirSync(OUT, { recursive: true })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let token = ''
let current = null  // the page in use: shot on a failure
async function api(method, url, body) {
  const r = await fetch(BASE + url, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(`${method} ${url}: HTTP ${r.status} ${await r.text()}`)
  return r.json()
}

async function settings(theme, guideOptedOut) {
  await api('PUT', '/fd/settings', { settings: {
    'fd:theme': theme,
    // The panels as a newcomer finds them, except the queue: shown where it's the subject.
    'fd:simple-left-open': 'true', 'fd:simple-right-open': 'true', 'fd:simple-queue-open': 'false',
    'fd:onboarding': JSON.stringify({ completed: { desktop: false, forge: false, council: false, kiosk: guideOptedOut }, dismissedHints: [] }),
  } })
}

async function signIn(page) {
  await page.goto(BASE + '/')
  await page.fill('input[type=email]', EMAIL)
  await page.fill('input[type=password]', PASSWORD)
  await page.click('button[type=submit]')
  await page.getByText('Research Assistant').first().waitFor()
}

// A dialog cut off below `bottom` (the last thing that matters in it).
async function shootDialog(page, dialog, name, bottom, pad = 20) {
  const box = await dialog.boundingBox()
  let height = box.height
  if (bottom) {
    const b = await bottom.boundingBox()
    height = Math.min(box.height, b.y + b.height - box.y + pad)
  }
  await page.screenshot({ path: path.join(OUT, name + '.png'), clip: { x: box.x, y: box.y, width: box.width, height } })
}

async function assets(browser) {
  for (const theme of ['dark', 'light']) {
    await settings(theme, true)
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: theme })
    // Flight Deck's copy of each chat stays out of it: the queued task the dark
    // pass sends would otherwise come back in the light one.
    await context.route('**/fd/chat/sessions**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: route.request().method() === 'GET' ? '[]' : '{}' }))
    const page = current = await context.newPage()
    await signIn(page)

    // The deck with a conversation open.
    await page.locator('aside button', { hasText: 'Research Assistant' }).first().click()
    await page.getByText('position against').waitFor()
    await page.getByText('ebike-subscriptions.md').first().waitFor()
    await sleep(800)
    await page.screenshot({ path: path.join(OUT, `overview-${theme}.png`) })

    // At work: the first queued task under way (the demo agent never finishes
    // it), the next one waiting, a question typed, files on the right.
    await page.locator('button[aria-label="Show queue"]').click()
    const queue = page.locator('textarea[placeholder^="Queue a message"]')
    for (const task of ['Draft a one-page pricing memo for the board from the comparison',
      'Check Dance and Rebike prices again next Monday and tell me what changed']) {
      await queue.fill(task)
      await page.getByRole('button', { name: 'Add', exact: true }).click()
    }
    await page.locator('textarea[placeholder^="Message"]').fill('Which of the three has the best repair service in Munich?')
    await sleep(500)
    const main = await page.locator('main').first().boundingBox()
    await page.screenshot({ path: path.join(OUT, `chat-${theme}.png`), clip: { x: main.x, y: main.y, width: 1280 - main.x, height: main.height } })
    await page.locator('textarea[placeholder^="Message"]').fill('')

    // New agent: an archetype picked, its name filled in.
    await page.locator('button[title="New agent from an archetype"]').first().click()
    const spawn = page.locator('[role=dialog][aria-label="New agent from an archetype"]')
    await spawn.locator('ul button').first().waitFor()
    const roles = await spawn.locator('ul button span.font-medium').allTextContents()
    if (theme === 'dark') console.log('archetypes:', roles.join(' | '))
    const pick = spawn.locator('ul button', { hasText: /research/i }).first()
    await ((await pick.count()) ? pick : spawn.locator('ul button').first()).click()
    await (await pick.count() ? pick : spawn.locator('ul button').first()).scrollIntoViewIfNeeded()
    await sleep(300)
    await shootDialog(page, spawn, `new-agent-${theme}`)
    await page.keyboard.press('Escape')

    // Settings.
    await page.locator('aside button', { hasText: 'Settings' }).click()
    const set = page.locator('[role=dialog][aria-label="Settings"]')
    await set.waitFor()
    await sleep(300)
    await shootDialog(page, set, `settings-${theme}`)

    // Profile, filled in: all three fields, so a taller window for it.
    await page.setViewportSize({ width: 1280, height: 1150 })
    await set.getByRole('button', { name: /Profile/ }).click()
    const prof = page.locator('[role=dialog][aria-label="Profile"]')
    await prof.getByText('My standing preferences').waitFor()
    await sleep(600)
    await shootDialog(page, prof, `profile-${theme}`, prof.locator('textarea').nth(2), 40)
    await page.keyboard.press('Escape')
    await page.setViewportSize({ width: 1280, height: 800 })

    // Connections: Google, then Telegram.
    await page.locator('aside button', { hasText: 'Settings' }).click()
    await set.getByRole('button', { name: /Connections/ }).click()
    const conn = page.locator('[role=dialog][aria-label="Connections"]')
    await conn.locator('#connections-tab-google').click()
    const google = conn.locator('#connections-panel-google')
    await google.getByRole('button', { name: 'Connect Google' }).waitFor()
    await sleep(500)
    await shootDialog(page, conn, `google-${theme}`, google.locator('> div').first())

    await conn.locator('#connections-tab-telegram').click()
    const tg = conn.locator('#connections-panel-telegram')
    // Before Connect: the bot's one-time link needs a live bot, which this deck hasn't got.
    await tg.getByRole('button', { name: 'Connect', exact: true }).waitFor()
    await sleep(500)
    await shootDialog(page, conn, `telegram-${theme}`, tg.locator('> div').first())
    await context.close()
  }
}

const results = []
function check(name, ok, detail) {
  results.push({ name, ok: !!ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail !== undefined ? '  — ' + JSON.stringify(detail) : ''}`)
}

async function guide(browser) {
  const guideDialog = (page) => page.locator('[role=dialog][aria-label="Getting started"]')
  for (const [form, viewport, theme] of [['wide', { width: 1280, height: 800 }, 'dark'], ['wide', { width: 1280, height: 800 }, 'light'], ['phone', { width: 390, height: 844 }, 'dark']]) {
    await settings(theme, false)
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme, hasTouch: form === 'phone', isMobile: form === 'phone' })
    const page = current = await context.newPage()
    await signIn(page)
    const g = guideDialog(page)
    await g.waitFor({ timeout: 10000 }).catch(() => {})
    check(`${form}/${theme}: opens by itself on first visit`, await g.isVisible())
    await sleep(1200)  // /fd/info: steps for what the deck offers
    const total = Number((await g.locator('h2 + span').textContent()).split(' of ')[1])
    check(`${form}/${theme}: all seven topics on a deck with Google, Microsoft and Telegram`, total === 7, total)
    for (let i = 1; i <= total; i++) {
      await sleep(400)
      await page.screenshot({ path: path.join(OUT, `${form}-${theme}-${i}.png`) })
      const img = g.locator('img').first()
      if (await img.count()) {
        const loaded = await img.evaluate((el) => el.complete && el.naturalWidth > 100)
        check(`${form}/${theme}: step ${i} screenshot loads`, loaded)
      }
      if (i < total) await g.getByRole('button', { name: 'Next', exact: true }).click()
    }
    if (form === 'wide' && theme === 'dark') {
      // Enlarge, then a step's button: the guide gives way to Connections on Google.
      await g.getByRole('button', { name: 'Back', exact: true }).click()
      await page.locator('nav[aria-label=Topics] button', { hasText: 'Google' }).click()
      await g.getByRole('button', { name: 'Enlarge screenshot' }).click()
      await sleep(300)
      await page.screenshot({ path: path.join(OUT, 'wide-dark-zoom.png') })
      await page.keyboard.press('Escape')
      check('zoom closes on Escape, guide stays', await g.isVisible())
      await g.getByRole('button', { name: 'Connect Google', exact: true }).click()
      const conn = page.locator('[role=dialog][aria-label="Connections"]')
      await conn.waitFor()
      check('"Connect Google" opens Connections on the Google tab',
        (await conn.locator('#connections-tab-google').getAttribute('aria-selected')) === 'true' && !(await g.isVisible()))
      await page.keyboard.press('Escape')

      // Reopen from Info; tick "Do not show again"; it stays away after a reload.
      await page.locator('aside button[aria-label=Info]').click()
      await page.locator('[role=dialog][aria-label=Info]').getByRole('button', { name: /Getting started/ }).click()
      check('Info → Getting started reopens it', await g.isVisible())
      await g.getByLabel('Do not show again').check()
      await sleep(800)  // the settings write is debounced
      const saved = await api('GET', '/fd/settings')
      check('"Do not show again" is saved to the user\'s settings', JSON.parse(saved['fd:onboarding'] || '{}').completed?.kiosk === true)
      await page.getByRole('button', { name: 'Close' }).first().click()
      await page.reload()
      await page.getByText('Research Assistant').first().waitFor()
      await sleep(2000)
      check('stays closed after a reload once opted out', !(await g.isVisible()))
      await page.locator('aside button', { hasText: 'Settings' }).click()
      await page.locator('[role=dialog][aria-label="Settings"]').getByRole('button', { name: /Getting started/ }).click()
      check('Settings → Getting started reopens it, box still ticked', await g.isVisible() && await g.getByLabel('Do not show again').isChecked())
    }
    await context.close()
  }
  fs.writeFileSync(path.join(RUN, 'report.json'), JSON.stringify(results, null, 2))
  if (results.some((r) => !r.ok)) process.exitCode = 1
}

;(async () => {
  token = (await api('POST', '/fd/auth/login', { email: EMAIL, password: PASSWORD })).access_token
  await api('PUT', '/fd/profile', {
    about_me: 'Pricing lead at Velora, a Berlin e-bike subscription start-up. I prepare the monthly pricing review.',
    company: 'Velora rents e-bikes by the month to city commuters in Berlin, Hamburg and Munich.',
    instructions: 'Answer in English. Keep it short: a table first, then a one-paragraph recommendation.',
  })
  const browser = await chromium.launch()
  try {
    if (MODE === 'guide') await guide(browser)
    else await assets(browser)
  } catch (e) {
    if (current) await current.screenshot({ path: path.join(RUN, 'failure.png') }).catch(() => {})
    throw e
  } finally {
    await browser.close()
  }
})().catch((e) => { console.error(e); process.exit(1) })
