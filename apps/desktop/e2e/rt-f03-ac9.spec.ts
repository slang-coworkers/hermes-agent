import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type MockBackendFixture,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { startMockServer } from './mock-server'
import { RealSessionBuilder } from './real-session-builder'
import { expect, test } from './test'

// AC-RT-F03-9 (desktop, mock inference server): the always-on native
// agent-shared Bot Chat lane. A coworker's canonical Bot Chat resolves to the
// SAME session across repeated opens (one conversation per coworker) WHILE each
// profile's Bot Chat is a DISTINCT session (isolation between profiles).
//
// Identity comes off the durable store, not the pixels: the canonical chat is a
// core UNIQUE(title) registry row titled exactly "Bot Chat" resolved by
// get_session_by_title (tui_gateway/methods_profiles.py:108); every message is
// tied to the session it was stored in.
//
// Setup BIRTHS each bot's canonical "Bot Chat" by seeding one marker turn
// through the real gateway (RealSessionBuilder); the test still RESOLVES + opens
// it via the real desktop→gateway exact-title path (row-menu "Open Bot Chat")
// and proves continuity from two SEPARATELY-SENT turns landing in one session id
// — never from the seed's mere existence. The per-bot seed marker is the
// readiness signal (present in the transcript on open AND re-open), since the
// fire-and-forget open (bot-row.tsx:287 voids openRosterBot) emits no DOM event.

type Page = MockBackendFixture['page']

const BOTS = [
  { name: 'rt-f03-bot-a', seed: 'rt-f03 seed alpha marker' },
  { name: 'rt-f03-bot-b', seed: 'rt-f03 seed bravo marker' }
] as const

const TOKENS = ['hello from bot a', 'hello from bot b', 'still bot a'] as const

let fixture: MockBackendFixture | null = null

// ── Diagnostics: reset per test (afterEach attaches on failure) ───────────
interface ReadEntry {
  ts: string
  db: string
  sql: string
  error: string | null
}
let readLog: ReadEntry[] = []
let phases: { phase: string; ms: number }[] = []
let uiObs: string[] = []

function resetDiagnostics(): void {
  readLog = []
  phases = []
  uiObs = []
}

async function timed<T>(phase: string, fn: () => Promise<T>): Promise<T> {
  const t0 = Date.now()

  try {
    return await fn()
  } finally {
    phases.push({ phase, ms: Date.now() - t0 })
  }
}

/** One read-only SELECT via the stdlib sqlite3 module (the image ships no
 *  sqlite3 CLI). Returns rows AND an error string instead of swallowing: a null
 *  result must be distinguishable from a failed read when classifying a flake. */
function pySelect(stateDb: string, sql: string, ...args: string[]): { rows: string[]; error: string | null } {
  if (!fs.existsSync(stateDb)) {
    readLog.push({ ts: new Date().toISOString(), db: stateDb, sql, error: 'db-missing' })

    return { rows: [], error: 'db-missing' }
  }

  const py =
    'import sqlite3,sys\n' +
    'c=sqlite3.connect("file:{}?mode=ro".format(sys.argv[1]),uri=True,timeout=5)\n' +
    'print("\\n".join(str(r[0]) for r in c.execute(sys.argv[2], tuple(sys.argv[3:]))))\n'

  try {
    const out = execFileSync('python3', ['-c', py, stateDb, sql, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe']
    })

    return {
      rows: out
        .split('\n')
        .map(s => s.trim())
        .filter(Boolean),
      error: null
    }
  } catch (error) {
    const message = String(
      (error as { stderr?: string }).stderr || (error as { message?: string }).message || error
    ).trim()

    readLog.push({ ts: new Date().toISOString(), db: stateDb, sql, error: message })

    return { rows: [], error: message }
  }
}

function botChatSessionIds(stateDb: string): string[] {
  return pySelect(stateDb, 'select id from sessions where title=?', 'Bot Chat').rows
}

function messageSessionId(stateDb: string, needle: string): string | null {
  const { rows } = pySelect(
    stateDb,
    'select session_id from messages where content like ? order by rowid desc limit 1',
    `%${needle}%`
  )

  return rows[0] ?? null
}

/** Assert `needle` is ABSENT from stateDb AND that the read itself succeeded.
 *  messageSessionId returns null on a silent SQLite read error too, so a bare
 *  toBeNull() on the isolation negatives would let a failed read pass as proof
 *  of isolation; here the read's error is checked explicitly. Runs at the
 *  quiescent end of the test, so a genuine error is a real failure to surface. */
function expectAbsent(stateDb: string, needle: string, message: string): void {
  const { rows, error } = pySelect(
    stateDb,
    'select session_id from messages where content like ? order by rowid desc limit 1',
    `%${needle}%`
  )

  expect(error, `${message} — SQLite read must succeed (a read error is not proof of absence)`).toBeNull()
  expect(rows[0] ?? null, message).toBeNull()
}

function stateDbFor(hermesHome: string, bot: string): string {
  return path.join(hermesHome, 'profiles', bot, 'state.db')
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** The roster row label displayName() renders for a profile: hyphens→spaces,
 *  title-cased, so "rt-f03-bot-a" surfaces as "Rt F03 Bot A" (labels.ts:60-62). */
function rosterLabel(name: string): string {
  return name
    .replace(/[-_]+/g, ' ')
    .trim()
    .replace(/\b\w/g, ch => ch.toUpperCase())
}

/** Birth one profile's canonical "Bot Chat" (core UNIQUE(title)) by running a
 *  single marker turn through the real gateway. session.create stores the title
 *  as pending_title and the first completed turn applies set_session_title, so
 *  the desktop resolves + opens THIS row via get_session_by_title('Bot Chat').
 *  The marker gives a per-bot readiness signal; its existence is NEVER asserted
 *  as continuity — the test sends its own turns for that. */
async function seedBot(hermesHome: string, mockUrl: string, name: string, seed: string): Promise<void> {
  const dir = path.join(hermesHome, 'profiles', name)
  fs.mkdirSync(dir, { recursive: true })
  writeMockProviderConfig(dir, mockUrl)
  writeEnvFile(dir)

  const builder = await RealSessionBuilder.start(dir)

  try {
    await builder.createSession({ title: 'Bot Chat', turns: [seed] })
  } finally {
    await builder.close()
  }
}

// ── Readiness / open / send helpers ───────────────────────────────────────

const focusedSurface = (page: Page) => page.locator('[data-chat-surface]:not([data-chat-unfocused])').first()

/** Pre-type readiness gate. The row-menu open is fire-and-forget (bot-row.tsx:287)
 *  and the composer stays editable while the gateway is "Reconnecting to Hermes…",
 *  so a send gated on composer-visibility alone can land on the previous surface
 *  or be lost. Gate instead on three signals of the focused, hydrated target
 *  surface: its own seed marker turn visible; the gateway open (the empty-composer
 *  "Voice dictation" control is enabled, and disabled while reconnecting); and an
 *  editable composer mounted. */
async function awaitBotChatReady(page: Page, seedMarker: string, timeout = 45_000): Promise<void> {
  const surface = focusedSurface(page)
  await expect(surface.getByText(seedMarker, { exact: true }).first()).toBeVisible({ timeout })
  await expect(surface.getByRole('button', { name: 'Voice dictation' })).toBeEnabled({ timeout })
  await expect(
    surface.locator('[data-slot="composer-root"] [contenteditable="true"]').filter({ visible: true }).first()
  ).toBeVisible({ timeout })
}

async function openBots(page: Page): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'Bots', exact: true })
    .or(page.getByRole('tab', { name: 'Bots', exact: true }))
    .first()

  await tab.click()
  await expect(page.getByRole('button', { name: 'New bot or group chat' })).toBeVisible()
}

/** Open a bot's canonical Bot Chat via the explicit row-menu action (the real
 *  exact-title resolution path). Only the idempotent open gesture is retried,
 *  until awaitBotChatReady holds; message submission stays single-shot so a lost
 *  or wrong-routed write is never masked by a resend. */
async function openBotChat(page: Page, botName: string, seedMarker: string, attempts = 3): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    await openBots(page)

    const row = page
      .getByRole('button', { name: new RegExp(`^${escapeRegExp(rosterLabel(botName))}\\b`, 'i') })
      .filter({ visible: true })
      .first()

    await expect(row).toBeVisible({ timeout: 30_000 })

    for (let menu = 0; menu < 3; menu += 1) {
      await row.click({ button: 'right' })

      try {
        await page.getByRole('menuitem', { name: 'Open Bot Chat' }).click({ timeout: 10_000 })

        break
      } catch {
        await page.keyboard.press('Escape')
      }
    }

    try {
      await timed(`open:${botName}:a${attempt}`, () => awaitBotChatReady(page, seedMarker))

      return
    } catch (error) {
      if (attempt >= attempts) {
        throw error
      }
    }
  }
}

interface PersistResult {
  ok: boolean
  reason?: 'WRONG_ROUTE' | 'NO_OWNER_ROW'
  sessionId?: string
}

/** Poll BOTH profile stores together and decide on the first decisive read:
 *  the token in the OTHER profile's db → wrong-route (fail fast, no resend); in
 *  the owner's db → success; neither within the budget → no-owner-row (a hard
 *  failure the afterEach hook then classifies). */
async function pollPersistedOrWrongRoute(
  ownerDb: string,
  otherDb: string,
  text: string,
  timeoutMs: number
): Promise<PersistResult> {
  const deadline = Date.now() + timeoutMs

  for (;;) {
    if (messageSessionId(otherDb, text) !== null) {
      return { ok: false, reason: 'WRONG_ROUTE' }
    }

    const owner = messageSessionId(ownerDb, text)

    if (owner !== null) {
      return { ok: true, sessionId: owner }
    }

    if (Date.now() > deadline) {
      return { ok: false, reason: 'NO_OWNER_ROW' }
    }

    await new Promise(resolve => setTimeout(resolve, 1_000))
  }
}

/** Type + submit EXACTLY ONCE into the already-ready focused Bot Chat, then
 *  assert the message persists in ownerDb and never appears in otherDb. */
async function sendMessage(
  page: Page,
  ownerDb: string,
  otherDb: string,
  seedMarker: string,
  text: string
): Promise<void> {
  await awaitBotChatReady(page, seedMarker)
  const surface = focusedSurface(page)

  const composer = surface
    .locator('[data-slot="composer-root"] [contenteditable="true"]')
    .filter({ visible: true })
    .first()

  await composer.click()
  await composer.fill(text)
  await expect(composer, 'composer holds the full message before submit').toHaveText(text)

  const send = surface.locator('button[type="submit"][aria-label="Send"]').filter({ visible: true }).first()
  await expect(send).toBeEnabled({ timeout: 15_000 })
  await timed(`submit:${text}`, async () => {
    await send.click()
    await expect(surface.getByText(text, { exact: false }).first()).toBeVisible({ timeout: 30_000 })
    uiObs.push(`messageRendered:${text}`)
  })

  const result = await timed(`persist:${text}`, () => pollPersistedOrWrongRoute(ownerDb, otherDb, text, 60_000))

  if ((await surface.locator('button[type="submit"][aria-label="Stop"]').count()) === 0) {
    uiObs.push(`composerIdle:${text}`)
  }

  expect(
    result.ok,
    result.ok ? '' : `persistence "${text}": ${result.reason} (owner=${ownerDb} other=${otherDb})`
  ).toBe(true)
}

// ── Failure-diagnostics support (runs before afterAll deletes the sandbox) ──

function shell(cmd: string): string {
  try {
    return execFileSync('bash', ['-lc', cmd], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  } catch (error) {
    const e = error as { stdout?: string; stderr?: string; message?: string }

    return String(e.stdout || e.stderr || e.message || error).trim()
  }
}

function dbState(db: string): Record<string, unknown> {
  const sizes: Record<string, number | null> = {}

  for (const suffix of ['', '-wal', '-shm']) {
    const p = db + suffix
    sizes[suffix || 'db'] = fs.existsSync(p) ? fs.statSync(p).size : null
  }

  return {
    sizes,
    journalMode: pySelect(db, 'pragma journal_mode').rows[0] ?? null,
    sessions: pySelect(db, "select id||'|'||coalesce(title,'') from sessions").rows,
    botChat: botChatSessionIds(db)
  }
}

test.beforeEach(() => {
  resetDiagnostics()
})

test.beforeAll(async () => {
  const mock = await startMockServer()
  const sandbox = createSandbox('rtf03')
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  for (const bot of BOTS) {
    await seedBot(sandbox.hermesHome, mock.url, bot.name, bot.seed)
  }

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  fixture = {
    app,
    page,
    mock,
    mockUrl: mock.url,
    sandbox,
    cleanup: async () => {
      await app.close().catch(() => undefined)
      await mock.close()
      sandbox.cleanup()
    }
  }

  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

/** On failure only, attach the classification bundle while the sandbox is still
 *  on disk — df/inodes, per-phase timings, the SQLite readLog, per-profile
 *  db/wal/shm state, a cross-profile token search (surfaces a wrong-route write),
 *  the UI observations, and the owning gateway's acceptance/completion records
 *  (tui prompt accepted / tui turn finished, server.py:12779,13626). Together
 *  these separate a submit/route race from a product persistence defect. */
test.afterEach(async () => {
  const info = test.info()

  if (info.status === info.expectedStatus) {
    return
  }

  const home = fixture?.sandbox.hermesHome

  const bundle: Record<string, unknown> = {
    status: info.status,
    expected: info.expectedStatus,
    phases,
    uiObs,
    readLog,
    disk: {
      df: shell(`df -h ${home ?? '/workspace/agent'}`),
      inodes: shell(`df -i ${home ?? '/workspace/agent'}`)
    }
  }

  if (home) {
    const profiles: Record<string, unknown> = {}

    for (const bot of BOTS) {
      const db = stateDbFor(home, bot.name)
      const crossProfileTokens: Record<string, string | null> = {}

      for (const token of TOKENS) {
        crossProfileTokens[token] = messageSessionId(db, token)
      }

      const logDir = path.join(home, 'profiles', bot.name, 'logs')

      const gatewayEvents = fs.existsSync(logDir)
        ? shell(`tail -n 200 ${logDir}/*.log 2>/dev/null | grep -E "tui prompt accepted|tui turn finished" || true`)
        : 'no-log-dir'

      profiles[bot.name] = { db: dbState(db), crossProfileTokens, gatewayEvents }
    }

    bundle.profiles = profiles
    bundle.rootLogs = shell(`tail -n 80 ${home}/logs/*.log 2>/dev/null || true`)
  }

  await info.attach('rt-f03-ac9-diagnostics', {
    body: JSON.stringify(bundle, null, 2),
    contentType: 'application/json'
  })
})

test("AC-RT-F03-9: a coworker Bot Chat is one session across re-opens while each profile's Bot Chat is distinct", async () => {
  test.setTimeout(180_000)

  const { sandbox } = fixture!
  const page = fixture!.page
  const [botA, botB] = BOTS
  const dbA = stateDbFor(sandbox.hermesHome, botA.name)
  const dbB = stateDbFor(sandbox.hermesHome, botB.name)

  await test.step('G3 — both seeded coworkers appear in the Bots roster', async () => {
    await openBots(page)
    await page.screenshot({ path: test.info().outputPath('step-1-bots-roster.png') })

    for (const bot of BOTS) {
      const row = page
        .getByRole('button', { name: new RegExp(`^${escapeRegExp(rosterLabel(bot.name))}\\b`, 'i') })
        .filter({ visible: true })
        .first()

      await expect(row, `roster row for ${bot.name}`).toBeVisible({ timeout: 30_000 })
    }
  })

  await test.step('bot-a canonical Bot Chat (real exact-title open): send turn 1', async () => {
    await openBotChat(page, botA.name, botA.seed)
    await sendMessage(page, dbA, dbB, botA.seed, 'hello from bot a')
    await page.screenshot({ path: test.info().outputPath('step-2-bot-a-first.png') })
  })

  await test.step('bot-b canonical Bot Chat (distinct profile — navigate away): send its turn', async () => {
    await openBotChat(page, botB.name, botB.seed)
    await sendMessage(page, dbB, dbA, botB.seed, 'hello from bot b')
    await page.screenshot({ path: test.info().outputPath('step-3-bot-b.png') })
  })

  await test.step('bot-a canonical Bot Chat RE-opened (real path): send turn 2', async () => {
    await openBotChat(page, botA.name, botA.seed)
    await sendMessage(page, dbA, dbB, botA.seed, 'still bot a')
    await page.screenshot({ path: test.info().outputPath('step-4-bot-a-reopened.png') })
  })

  await test.step('G1/G2 — state.db: continuity across re-open + cross-profile isolation', async () => {
    const aFirst = messageSessionId(dbA, 'hello from bot a')
    const aSecond = messageSessionId(dbA, 'still bot a')
    expect(aFirst, 'bot-a turn 1 persisted in its own store').not.toBeNull()
    expect(aSecond, 're-opened Bot Chat routes a second, separately-sent turn to the SAME session').toBe(aFirst)
    expect(
      botChatSessionIds(dbA),
      'bot-a has exactly one canonical Bot Chat (== get_session_by_title) holding both sent turns'
    ).toEqual([aFirst])

    const bId = messageSessionId(dbB, 'hello from bot b')
    expect(bId, 'bot-b turn persisted in its own independent store').not.toBeNull()
    expect(botChatSessionIds(dbB), 'bot-b has exactly one canonical Bot Chat').toEqual([bId])
    expect(bId, "bot-b's Bot Chat is a distinct session from bot-a's").not.toBe(aFirst)

    expectAbsent(dbB, 'hello from bot a', 'bot-a text must never reach bot-b')
    expectAbsent(dbB, 'still bot a', 'bot-a text must never reach bot-b')
    expectAbsent(dbA, 'hello from bot b', 'bot-b text must never reach bot-a')
  })
})
