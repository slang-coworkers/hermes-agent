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

// AC-FLEET-F62-10 (desktop, stub). Scope boundary: the coworkers' podman sandbox
// (docker backend + OneCLI egress) is booted for real in AC-FLEET-F62-5/6; a bare
// Electron/Playwright harness has no podman host, so this tier drives only the roster
// and room UI behind a mock provider.

type Page = MockBackendFixture['page']

// Role metadata mirrors the fleet spec's types.<role>.ui_meta.
const COWORKERS = [
  { name: 'orchestrator', title: 'Orchestrator', description: 'Fleet orchestrator (elevated)', shape: 'circle' },
  { name: 'architect', title: 'Architect', description: 'Fleet architect', shape: 'square' },
  { name: 'builder', title: 'Builder', description: 'Fleet builder', shape: 'square' },
  { name: 'tester', title: 'Tester', description: 'Fleet tester', shape: 'square' },
  { name: 'reviewer', title: 'Reviewer', description: 'Fleet reviewer', shape: 'square' }
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

/** Create a room through the production New Group Chat flow, selecting `memberTitles`. */
async function createRoom(page: Page, groupName: string, memberTitles: readonly string[]): Promise<void> {
  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Group Chat' }).click()
  const dialog = page.getByRole('dialog', { name: 'New Group Chat' })
  for (const title of memberTitles) {
    await dialog.getByText(title, { exact: true }).locator('xpath=ancestor::label').getByRole('checkbox').click()
  }
  await dialog.getByRole('textbox', { name: 'Group name' }).fill(groupName)
  // The Create button's count is the selected-member count, so the exact label asserts membership.
  await dialog.getByRole('button', { name: `Create Group (${memberTitles.length})` }).click()
  await expect(dialog).toBeHidden({ timeout: 30_000 })
  await expect(
    page.getByRole('tab', { name: new RegExp(`${groupName}`) }).filter({ visible: true }).first()
  ).toBeVisible({ timeout: 30_000 })
}

/** Seed one coworker profile before launch: profile dir + mock provider + a durable
 *  canonical "Bot Chat", plus ui_meta['hermes-bots'] on profile.yaml so the roster
 *  renders it as a bot with its title + avatar. */
async function seedCoworker(
  hermesHome: string,
  mockUrl: string,
  bot: { name: string; title: string; description: string; shape: string }
): Promise<void> {
  const dir = path.join(hermesHome, 'profiles', bot.name)
  fs.mkdirSync(dir, { recursive: true })
  writeMockProviderConfig(dir, mockUrl)
  writeEnvFile(dir)

  const builder = await RealSessionBuilder.start(dir)
  try {
    await builder.createSession({ title: 'Bot Chat', turns: [`Hello ${bot.name}`] })
  } finally {
    await builder.close()
  }

  const profileYaml = path.join(dir, 'profile.yaml')
  let existing: Record<string, unknown> = {}
  if (fs.existsSync(profileYaml)) {
    try {
      existing = JSON.parse(fs.readFileSync(profileYaml, 'utf8')) as Record<string, unknown>
    } catch {
      existing = {}
    }
  }
  const uiMeta = { ...((existing.ui_meta as Record<string, unknown>) ?? {}) }
  uiMeta['hermes-bots'] = { title: bot.title, description: bot.description, shape: bot.shape }
  fs.writeFileSync(profileYaml, JSON.stringify({ ...existing, ui_meta: uiMeta }, null, 2), 'utf8')
}

/** Seed the launch/`default` host's profile.yaml with ui_meta['hermes-bots'].hidden:true so
 *  the desktop roster drops it from the VISIBLE set — the criterion's exactly-five is about
 *  coworkers, and the launch host is not one. isBotHidden reads the hidden flag off the merged
 *  meta, which mergeServerMeta lifts from ui_meta['hermes-bots'] (profile-ops.ts:238-252 →
 *  hidden-bots.ts:25-27 → roster-pane.tsx:336-337). ui_meta is RPC/fixture-owned, not
 *  distribution-rendered, so the fixture seeds it exactly as the operator would via
 *  profiles.configure. */
function seedDefaultHidden(hermesHome: string): void {
  const profileYaml = path.join(hermesHome, 'profile.yaml')
  let existing: Record<string, unknown> = {}
  if (fs.existsSync(profileYaml)) {
    try {
      existing = JSON.parse(fs.readFileSync(profileYaml, 'utf8')) as Record<string, unknown>
    } catch {
      existing = {}
    }
  }
  const uiMeta = { ...((existing.ui_meta as Record<string, unknown>) ?? {}) }
  const bots = { ...((uiMeta['hermes-bots'] as Record<string, unknown>) ?? {}) }
  bots.hidden = true
  uiMeta['hermes-bots'] = bots
  fs.writeFileSync(profileYaml, JSON.stringify({ ...existing, ui_meta: uiMeta }, null, 2), 'utf8')
}

test.beforeAll(async () => {
  const mock = await startMockServer()
  const sandbox = createSandbox('fleet-f62-ac10')
  // The DEFAULT (launch) profile is the multiplexer host; the five coworkers are the
  // served secondaries. All six live in ONE HERMES_HOME behind ONE gateway.
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  for (const bot of COWORKERS) {
    await seedCoworker(sandbox.hermesHome, mock.url, bot)
  }
  // The launch/multiplexer host renders as the `default` roster row; hide it so the
  // visible coworker roster is exactly the five (AC-10 option i, ADR § Deployment item 3).
  seedDefaultHidden(sandbox.hermesHome)

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

test('AC-FLEET-F62-10: the desktop app renders the five-coworker roster (launch default hidden), the two standing rooms, the orchestrator Bot Chat, and the Kanban board over one gateway', async () => {
  test.setTimeout(420_000) // step 4 adds a full app reload + re-ready; stay under the 10-min tier cap
  const { page, sandbox } = fixture!

  // Step 1 — Bots roster: exactly the five coworkers, each with its role metadata.
  await openBots(page)
  await page.screenshot({ path: test.info().outputPath('step-1-bots-roster.png') })
  for (const bot of COWORKERS) {
    const row = page
      .getByRole('button', { name: new RegExp(`^${bot.title}\\b`, 'i') })
      .filter({ visible: true })
      .first()
    await expect(row, `roster row for ${bot.name} (${bot.title}) is rendered`).toBeVisible({ timeout: 30_000 })
    await expect(
      row.locator(`[data-bot-face="${bot.name}"]`),
      `avatar element for ${bot.name} is present next to its title`
    ).toBeVisible()
  }
  // The operator-hidden launch `default` host must be ABSENT from the visible roster.
  // mergeServerMeta lifts ui_meta['hermes-bots'].hidden onto the meta snapshot only in a
  // post-paint effect (profile-ops.ts:238-252), so this auto-retrying assertion waits for
  // the default face to drop before the exact-count check below (which does not retry).
  await expect(
    page.locator('[data-bot-face="default"]'),
    'launch default host is hidden from the visible roster'
  ).toHaveCount(0)
  // Exactly the five coworkers render as bots — no unexpected sixth and no launch host.
  // data-bot-face tags each rendered avatar by profile name; dedupe because the
  // auto-selected bot's face may also render in the workspace header.
  const faceNames = await page
    .locator('[data-bot-face]')
    .evaluateAll(nodes => Array.from(new Set(nodes.map(n => n.getAttribute('data-bot-face')).filter(Boolean))))
  expect(faceNames.slice().sort(), 'roster shows exactly the five coworkers, no other profile').toEqual(
    COWORKERS.map(bot => bot.name).slice().sort()
  )
  // Role descriptions are carried on profile.yaml (bot-row renders title+avatar, not
  // description), so assert each coworker's rendered role metadata there.
  for (const bot of COWORKERS) {
    const raw = fs.readFileSync(path.join(sandbox.hermesHome, 'profiles', bot.name, 'profile.yaml'), 'utf8')
    expect(raw, `${bot.name} profile.yaml carries its role title`).toContain(bot.title)
    expect(raw, `${bot.name} profile.yaml carries its role description`).toContain(bot.description)
  }

  // Step 2 — the two standing rooms, via the production New Group Chat flow.
  await createRoom(page, 'fleet-room', ['Orchestrator', 'Architect', 'Builder', 'Tester', 'Reviewer'])
  await createRoom(page, 'review-room', ['Builder', 'Tester', 'Reviewer'])
  await expect(page.getByRole('tab', { name: /fleet-room/ }).filter({ visible: true }).first()).toBeVisible()
  await expect(page.getByRole('tab', { name: /review-room/ }).filter({ visible: true }).first()).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('step-2-standing-rooms.png') })

  // Step 3 — open the orchestrator's Bot Chat; it opens over the single gateway.
  const orchestratorRow = page
    .getByRole('button', { name: /^Orchestrator\b/i })
    .filter({ visible: true })
    .first()
  await orchestratorRow.click()
  await expect(
    page.getByRole('tab', { name: /Bot Chat/ }).filter({ visible: true }).first()
  ).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: test.info().outputPath('step-3-orchestrator-bot-chat.png') })

  // Step 4 — the Kanban board renders over the same single gateway. The desktop Kanban
  // plugin is opt-in (ships disabled), so persist its enable decision and reload: plugin
  // discovery re-runs at module init and registers its /kanban route + sidebar entry only
  // when the persisted decision enables it.
  await page.evaluate(() =>
    window.localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ kanban: true }))
  )
  await page.reload()
  await waitForAppReady(fixture!, 120_000)
  await page.getByRole('button', { name: 'Kanban' }).click()
  // The board page always renders its <h1>Kanban</h1> header (board.tsx:1331); the
  // "No tasks on this board" empty-state paints only after the bundled gateway plugin's
  // /api/plugins/kanban/board returns 200 (board.tsx:1378-1388) — proving it loaded over the
  // one connection, not the ErrorState. (Scope to the level-1 heading: the nav row and the
  // board-switcher button are also labelled "Kanban".)
  await expect(page.getByRole('heading', { name: 'Kanban', level: 1 })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('No tasks on this board')).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: test.info().outputPath('step-4-kanban-board.png') })

  // One gateway for the whole app: the fixture launches a single local backend and
  // waitForAppReady gated on the one gateway becoming ready; the statusbar reads it.
  await expect(
    page.locator('[data-slot="statusbar"]').getByText('ready', { exact: true })
  ).toBeVisible({ timeout: 60_000 })
})
