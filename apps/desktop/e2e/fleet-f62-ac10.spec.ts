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

// Each coworker profile's directory name is its @handle; `title` is the role title
// the app must render from the profile's configured top-level `display_name`
// (labels.ts:42-44) — non-handle-derivable, so it cannot come from the title-cased
// profile-name fallback (labels.ts:61-63).
const COWORKERS = [
  { name: 'orchestrator', title: 'Fleet Orchestrator' },
  { name: 'architect', title: 'Systems Architect' },
  { name: 'builder', title: 'Implementation Builder' },
  { name: 'tester', title: 'Verification Tester' },
  { name: 'reviewer', title: 'Release Reviewer' }
] as const

let fixture: MockBackendFixture | null = null

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

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
 *  canonical "Bot Chat", plus a TOP-LEVEL `display_name` on its profile.yaml so the
 *  roster renders its role title (labels.ts:42-44 reads bot.display_name; no
 *  ui_meta['hermes-bots'].title is set, so the title is proven to come from the
 *  configured display_name, not the title-cased handle fallback). */
async function seedCoworker(
  hermesHome: string,
  mockUrl: string,
  bot: { name: string; title: string }
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

  writeProfileYaml(path.join(dir, 'profile.yaml'), { display_name: bot.title })
}

/** Seed the launch/`default` host's TOP-LEVEL profile.yaml (Hermes treats $HERMES_HOME
 *  itself as the `default` profile and IGNORES $HERMES_HOME/profiles/default —
 *  profiles.py:1035,:1068) with ui_meta['hermes-bots'].hidden:true so the desktop roster
 *  drops it from the VISIBLE set (isBotHidden reads the merged hidden flag lifted from
 *  profile.yaml's ui_meta — methods_profiles.py:297-306 → hidden-bots.ts:25-27 →
 *  roster-pane.tsx:336-337). The criterion's exactly-five is about coworkers; the launch
 *  host is not one. */
function seedDefaultHidden(hermesHome: string): void {
  const profileYaml = path.join(hermesHome, 'profile.yaml')
  const existing = readProfileYaml(profileYaml)
  const uiMeta = { ...((existing.ui_meta as Record<string, unknown>) ?? {}) }
  const bots = { ...((uiMeta['hermes-bots'] as Record<string, unknown>) ?? {}) }
  bots.hidden = true
  uiMeta['hermes-bots'] = bots
  writeProfileYaml(profileYaml, { ...existing, ui_meta: uiMeta })
}

/** profile.yaml is YAML, and any JSON object is valid YAML, so JSON round-trips through
 *  yaml.safe_load on the gateway side. Merge onto any existing content. */
function readProfileYaml(file: string): Record<string, unknown> {
  if (!fs.existsSync(file)) return {}
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>
  } catch {
    return {}
  }
}

function writeProfileYaml(file: string, data: Record<string, unknown>): void {
  fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf8')
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

test('AC-FLEET-F62-10: the desktop app renders exactly the five-coworker roster (launch default hidden), each row bound 1:1 to its role title + @handle, the two standing rooms, the orchestrator Bot Chat, and the Kanban board over one gateway', async () => {
  test.setTimeout(420_000) // step 4 adds a Settings toggle + Kanban board fetch; stay under the 10-min tier cap
  const { page } = fixture!

  // Step 1 — Bots roster: exactly the five coworkers, each row bound 1:1 to its role
  // title and @handle, launch `default` host hidden.
  await openBots(page)
  await page.screenshot({ path: test.info().outputPath('step-1-bots-roster.png') })

  // The operator-hidden launch `default` host must be ABSENT from the roster.
  await expect(
    page.locator('[data-bot-face="default"]'),
    'launch default host is hidden from the visible roster'
  ).toHaveCount(0)

  // Row-scoped 1:1 title<->handle binding. The row button's accessible name is the
  // composite rowTooltip `<display_name> · @<handle> · <gateway> · <status>`
  // (bot-row.tsx:179-181,211-212), so anchoring on `^<title> · @<handle>` binds each
  // configured display_name to ITS handle; the `(?: ·|$)` boundary rejects a
  // prefix-of-a-longer-title match and tolerates the trailing gateway/status segments.
  // A global getByText(title) + a separate @handle check could BOTH pass with the title
  // on the wrong row, so this locates one unique row per handle and asserts its whole name.
  for (const bot of COWORKERS) {
    const nameRe = new RegExp('^' + escapeRegex(bot.title) + ' · @' + escapeRegex(bot.name) + '(?: ·|$)')
    const row = page.getByRole('button', { name: nameRe })
    await expect(row, `exactly one roster row named "${bot.title} · @${bot.name}"`).toHaveCount(1)
    await expect(
      row.locator(`[data-bot-face="${bot.name}"]`),
      `the ${bot.name} row carries its own avatar face`
    ).toHaveCount(1)
  }

  // Exactly five coworker rows and no unexpected sixth: every roster row button's
  // accessible name carries the ` · @<handle>` segment, so counting that pattern counts
  // the bot rows (the "New bot or group chat" button and room tabs do not match it).
  await expect(
    page.getByRole('button', { name: /·\s@/ }),
    'the visible roster is exactly the five coworkers'
  ).toHaveCount(COWORKERS.length)

  // Step 2 — the two standing rooms, via the production New Group Chat flow.
  await createRoom(page, 'fleet-room', COWORKERS.map(b => b.title))
  await createRoom(page, 'review-room', ['Implementation Builder', 'Verification Tester', 'Release Reviewer'])
  await expect(page.getByRole('tab', { name: /fleet-room/ }).filter({ visible: true }).first()).toBeVisible()
  await expect(page.getByRole('tab', { name: /review-room/ }).filter({ visible: true }).first()).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('step-2-standing-rooms.png') })

  // Step 3 — open the orchestrator's Bot Chat; it opens over the single gateway.
  const orchestratorRow = page.getByRole('button', {
    name: new RegExp('^' + escapeRegex('Fleet Orchestrator') + ' · @orchestrator(?: ·|$)')
  })
  await orchestratorRow.click()
  await expect(
    page.getByRole('tab', { name: /Bot Chat/ }).filter({ visible: true }).first()
  ).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: test.info().outputPath('step-3-orchestrator-bot-chat.png') })

  // Step 4 — enable the opt-in Kanban plugin through the product's Settings ▸ Plugins
  // Switch (plugins-settings.tsx:328-335; the Switch name flips "Enable Kanban" ->
  // "Disable Kanban") and render its board. Arm the board-response listener BEFORE
  // activation: enabling mounts the sidebar KanbanCount, which fetches the same
  // boardKey(slug,false) the board page uses and the query client holds fresh for 60s
  // (query-client.ts:10), so a listener armed only around the nav click could miss the
  // single real request and hang.
  await page.evaluate(() => {
    window.location.hash = '/settings?tab=plugins'
  })
  const enableKanban = page.getByRole('switch', { name: 'Enable Kanban' })
  await expect(enableKanban).toBeVisible({ timeout: 30_000 })
  const [boardResp] = await Promise.all([
    page.waitForResponse(
      response =>
        response.request().method() === 'GET' &&
        /\/api\/plugins\/kanban\/board(?:\?|$)/.test(response.url()),
      { timeout: 60_000 }
    ),
    (async () => {
      await enableKanban.click()
      await expect(page.getByRole('switch', { name: 'Disable Kanban' })).toBeVisible({ timeout: 30_000 })
      const kanbanNav = page.getByRole('button', { name: 'Kanban', exact: true })
      await expect(kanbanNav).toBeVisible({ timeout: 30_000 })
      await kanbanNav.click()
    })()
  ])
  expect(boardResp.status(), 'GET /api/plugins/kanban/board returned 200').toBe(200)
  await expect(page.getByRole('heading', { name: 'Kanban', level: 1 })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('No tasks on this board')).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: test.info().outputPath('step-4-kanban-board.png') })

  // Single-gateway invariant: the fixture launches exactly ONE local backend
  // (buildAppEnv(sandbox)), waitForAppReady gated on that one gateway, and the Kanban
  // board 200 above is served by that same gateway — there is no second port anywhere.
})
