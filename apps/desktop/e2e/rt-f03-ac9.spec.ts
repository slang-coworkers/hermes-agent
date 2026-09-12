import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

// AC-RT-F03-9 (desktop, mock inference server): the always-on native
// agent-shared Bot Chat lane. A coworker's canonical Bot Chat resolves to the
// SAME session across repeated opens (one conversation per coworker) WHILE each
// profile's Bot Chat is a DISTINCT session (isolation between profiles). This
// lane is independent of the session-mode flags nv-coworker-compose renders.
//
// Identity comes off the durable store, not the pixels: the canonical chat is a
// core UNIQUE(title) registry row titled exactly "Bot Chat"
// (tui_gateway/methods_profiles.py:89,108), and every message is tied to the
// session it was stored in — so "same session across re-opens" is proven by two
// separately-sent messages landing in one session, not by the tab staying open.

type Page = MockBackendFixture['page']

const BOTS = [
  { name: 'rt-f03-bot-a', title: 'RT F03 Bot A' },
  { name: 'rt-f03-bot-b', title: 'RT F03 Bot B' }
] as const

let fixture: MockBackendFixture | null = null

async function openBots(page: Page): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'Bots', exact: true })
    .or(page.getByRole('tab', { name: 'Bots', exact: true }))
    .first()
  await tab.click()
  await expect(page.getByRole('button', { name: 'New bot or group chat' })).toBeVisible()
}

async function createAgent(page: Page, name: string, title: string): Promise<void> {
  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Bot' }).click()
  const dialog = page.getByRole('dialog', { name: 'New Bot' })
  await dialog.getByPlaceholder('inbox-triage').fill(name)
  await dialog.getByPlaceholder('Inbox Triage').fill(title)
  await dialog.getByRole('button', { name: 'Create Bot' }).click()
  await expect(dialog).toBeHidden({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: new RegExp(`^${title}\\b`) }).first()).toBeVisible({
    timeout: 30_000
  })
}

/** Open a bot's canonical forever-chat via the explicit row-menu action, which
 *  always targets the "Bot Chat" registry (openRosterBot canonical:true) — a
 *  plain row click is "go to this bot" and may resume another workspace. */
async function openBotChat(page: Page, title: string): Promise<void> {
  const row = page
    .getByRole('button', { name: new RegExp(`^${title}\\b`) })
    .filter({ visible: true })
    .first()
  await expect(row).toBeVisible({ timeout: 30_000 })
  for (let attempt = 0; attempt < 3; attempt++) {
    await row.click({ button: 'right' })
    try {
      await page.getByRole('menuitem', { name: 'Open Bot Chat' }).click({ timeout: 10_000 })
      break
    } catch {
      await page.keyboard.press('Escape')
    }
  }
  // First open spawns the bot's own backend, so the composer can take a moment.
  await page
    .locator('[data-slot="composer-root"] [contenteditable="true"]')
    .filter({ visible: true })
    .first()
    .waitFor({ state: 'visible', timeout: 120_000 })
}

async function sendMessage(page: Page, text: string): Promise<void> {
  const composer = page
    .locator('[data-slot="composer-root"] [contenteditable="true"]')
    .filter({ visible: true })
    .first()
  await composer.click()
  await composer.type(text, { delay: 20 })
  await page.keyboard.press('Enter')
  // The user message is persisted on submit (independent of the model reply).
  await page.waitForFunction(t => (document.body?.textContent ?? '').includes(t), text, {
    timeout: 30_000
  })
}

/** Run one read-only SELECT against a profile's state.db via the stdlib sqlite3
 *  module (the image ships no sqlite3 CLI); returns column 0 of each row. */
function pySelect(stateDb: string, sql: string, ...args: string[]): string[] {
  if (!fs.existsSync(stateDb)) return []
  const py =
    'import sqlite3,sys\n' +
    'try:\n' +
    '  c=sqlite3.connect("file:{}?mode=ro".format(sys.argv[1]),uri=True,timeout=5)\n' +
    '  print("\\n".join(str(r[0]) for r in c.execute(sys.argv[2], tuple(sys.argv[3:]))))\n' +
    'except Exception:\n' +
    '  pass\n'
  try {
    return execFileSync('python3', ['-c', py, stateDb, sql, ...args], { encoding: 'utf8' })
      .split('\n')
      .map(s => s.trim())
      .filter(Boolean)
  } catch {
    return []
  }
}

function botChatSessionIds(stateDb: string): string[] {
  return pySelect(stateDb, 'select id from sessions where title=?', 'Bot Chat')
}

function messageSessionId(stateDb: string, needle: string): string | null {
  const rows = pySelect(
    stateDb,
    'select session_id from messages where content like ? order by rowid desc limit 1',
    `%${needle}%`
  )
  return rows[0] ?? null
}

function stateDbFor(hermesHome: string, bot: string): string {
  return path.join(hermesHome, 'profiles', bot, 'state.db')
}

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('AC-RT-F03-9: a coworker Bot Chat is one session across re-opens while each profile\'s Bot Chat is distinct', async () => {
  test.setTimeout(300_000)
  const { page, sandbox } = fixture!
  const dbA = stateDbFor(sandbox.hermesHome, 'rt-f03-bot-a')
  const dbB = stateDbFor(sandbox.hermesHome, 'rt-f03-bot-b')

  await test.step('both coworkers appear in the Bots roster', async () => {
    await openBots(page)
    for (const bot of BOTS) {
      await createAgent(page, bot.name, bot.title)
    }
    await page.screenshot({ path: test.info().outputPath('step-1-bots-roster.png') })
    for (const bot of BOTS) {
      await expect(
        page.getByRole('button', { name: new RegExp(`^${bot.title}\\b`) }).first(),
        `roster row for ${bot.name}`
      ).toBeVisible()
    }
  })

  await test.step('bot-a first open: send a message into its Bot Chat', async () => {
    await openBotChat(page, 'RT F03 Bot A')
    await sendMessage(page, 'hello from bot a')
    await page.screenshot({ path: test.info().outputPath('step-2-bot-a-first.png') })
  })

  await test.step('bot-b open (distinct profile): send a message into its Bot Chat', async () => {
    await openBots(page)
    await openBotChat(page, 'RT F03 Bot B')
    await sendMessage(page, 'hello from bot b')
    await page.screenshot({ path: test.info().outputPath('step-3-bot-b.png') })
  })

  await test.step('bot-a re-open via the explicit action: send again', async () => {
    await openBots(page)
    await openBotChat(page, 'RT F03 Bot A')
    await sendMessage(page, 'still bot a')
    await page.screenshot({ path: test.info().outputPath('step-4-bot-a-reopened.png') })
  })

  await test.step('state.db proves within-profile continuity and between-profile isolation', async () => {
    await expect
      .poll(() => messageSessionId(dbA, 'hello from bot a'), { timeout: 120_000 })
      .not.toBeNull()
    await expect
      .poll(() => messageSessionId(dbA, 'still bot a'), { timeout: 120_000 })
      .not.toBeNull()
    const aFirst = messageSessionId(dbA, 'hello from bot a')
    const aSecond = messageSessionId(dbA, 'still bot a')
    expect(aSecond, 'a re-opened Bot Chat routes to the same session').toBe(aFirst)
    expect(botChatSessionIds(dbA), 'bot-a has exactly one canonical Bot Chat, holding both turns').toEqual([
      aFirst
    ])

    await expect
      .poll(() => messageSessionId(dbB, 'hello from bot b'), { timeout: 120_000 })
      .not.toBeNull()
    const bId = messageSessionId(dbB, 'hello from bot b')
    expect(botChatSessionIds(dbB), 'bot-b has exactly one canonical Bot Chat').toEqual([bId])
    expect(bId, 'bot-b Bot Chat is a distinct session from bot-a').not.toBe(aFirst)
  })
})
