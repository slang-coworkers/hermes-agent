import fs from 'node:fs'
import path from 'node:path'

import type { ElectronApplication, Response } from '@playwright/test'

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

// Shared driver for the FLEET-F62 desktop tier (R5-1). The counted spec
// (fleet-f62-ac10.spec.ts) and the uncounted navigation preflight
// (fleet-f62-ac10-preflight.spec.ts) call the SAME driveFleetF62Nav here, so the
// nav path the preflight proves is byte-for-byte the path the counted run drives —
// a new nav bug cannot first surface mid-counted-run.

type Page = MockBackendFixture['page']

// Each coworker profile's directory name is its @handle; `title` is the role title
// the app must render from the profile's configured top-level `display_name`
// (labels.ts:42-44) — non-handle-derivable, so it cannot come from the title-cased
// profile-name fallback (labels.ts:61-63).
export const COWORKERS = [
  { name: 'orchestrator', title: 'Fleet Orchestrator' },
  { name: 'architect', title: 'Systems Architect' },
  { name: 'builder', title: 'Implementation Builder' },
  { name: 'tester', title: 'Verification Tester' },
  { name: 'reviewer', title: 'Release Reviewer' }
] as const

// review-room membership: builder, tester, reviewer (the three review-pipeline roles).
export const REVIEW_ROOM_TITLES = ['Implementation Builder', 'Verification Tester', 'Release Reviewer'] as const

export function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// The row button's accessible name is the composite rowTooltip
// `<display_name> · @<handle> · <gateway> · <status>` (bot-row.tsx:179-181,211-212);
// anchoring on `^<title> · @<handle>` binds each configured title to ITS handle and the
// `(?: ·|$)` boundary rejects a prefix-of-a-longer-title match while tolerating the
// trailing gateway/status segments.
export function rowNameRegex(title: string, handle: string): RegExp {
  return new RegExp('^' + escapeRegex(title) + ' · @' + escapeRegex(handle) + '(?: ·|$)')
}

/** Optional per-step assertions the caller injects into the shared nav path. The
 *  counted spec passes callbacks that assert the AC-FLEET-F62-10 criteria; the
 *  preflight passes mechanical-only callbacks (or none) and relies on the driver's own
 *  locators/waits as proof that each action completed. */
export interface FleetF62NavCheckpoints {
  afterBotsRoster?: (page: Page) => Promise<void>
  afterRooms?: (page: Page) => Promise<void>
  afterOrchestratorChat?: (page: Page) => Promise<void>
  afterKanbanBoard?: (page: Page, boardResponse: Response) => Promise<void>
}

// ─── Seeding (identical fixture for both specs) ──────────────────────────

/** profile.yaml is YAML, and any JSON object is valid YAML, so JSON round-trips through
 *  yaml.safe_load on the gateway side. Merge onto any existing content. */
function readProfileYaml(file: string): Record<string, unknown> {
  if (!fs.existsSync(file)) {return {}}

  try {
    return JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>
  } catch {
    return {}
  }
}

function writeProfileYaml(file: string, data: Record<string, unknown>): void {
  fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf8')
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

/** Seed the six-profile fleet (launch `default` hidden + the five coworkers) into a
 *  FRESH disposable sandbox root — createSandbox gives ONE tmp root holding both
 *  HERMES_HOME (hermes-home) and HERMES_DESKTOP_USER_DATA_DIR (electron-user-data),
 *  and cleanup() destroys the whole root — then launch a NEW Electron + gateway (R5-1
 *  state isolation: the preflight and the counted spec pass DISTINCT prefixes, so
 *  neither the room DB nor the desktop plugin-decision store carries over). Fail-closed:
 *  a partial-start exception closes the app + mock and destroys the root before it
 *  rethrows, so a boot failure leaks no resources or half-seeded root. */
export async function bootFleetDesktop(prefix: string): Promise<MockBackendFixture> {
  const mock = await startMockServer()
  const sandbox = createSandbox(prefix)
  let app: ElectronApplication | undefined

  try {
    // The DEFAULT (launch) profile is the multiplexer host; the five coworkers are the
    // served secondaries. All six live in ONE HERMES_HOME behind ONE gateway.
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)

    for (const bot of COWORKERS) {
      await seedCoworker(sandbox.hermesHome, mock.url, bot)
    }

    seedDefaultHidden(sandbox.hermesHome)

    const launched = await launchDesktop(buildAppEnv(sandbox))
    app = launched.app

    const fixture: MockBackendFixture = {
      app: launched.app,
      page: launched.page,
      mock,
      mockUrl: mock.url,
      sandbox,
      cleanup: async () => {
        await launched.app.close().catch(() => undefined)
        await mock.close()
        sandbox.cleanup()
      }
    }

    await waitForAppReady(fixture, 120_000)

    return fixture
  } catch (err) {
    if (app) {await app.close().catch(() => undefined)}
    await mock.close().catch(() => undefined)
    sandbox.cleanup()
    throw err
  }
}

// ─── Navigation actions (owned by the driver; both specs share them) ─────

async function openBots(page: Page): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'Bots', exact: true })
    .or(page.getByRole('tab', { name: 'Bots', exact: true }))
    .first()

  await tab.click()
  await expect(page.getByRole('button', { name: 'New bot or group chat' })).toBeVisible({ timeout: 30_000 })
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

async function openOrchestratorChat(page: Page): Promise<void> {
  await page.getByRole('button', { name: rowNameRegex('Fleet Orchestrator', 'orchestrator') }).click()
  await expect(
    page.getByRole('tab', { name: /Bot Chat/ }).filter({ visible: true }).first()
  ).toBeVisible({ timeout: 30_000 })
}

/** Dismiss the full-viewport Settings ▸ Plugins OverlayView backdrop (fixed inset-0
 *  z-50 data-overlay-surface, overlay-view.tsx:74-104) BEFORE touching the Sessions tab:
 *  while it is open it intercepts pointer events on the panes beneath it, so a
 *  Sessions-tab click is intercepted and times out (the round-4 FAIL). Prefer the labeled
 *  Close-settings control (overlay-view.tsx:126-134), fall back to Escape
 *  (overlay-view.tsx:53-72), then await the surface's removal. */
async function dismissSettingsOverlay(page: Page): Promise<void> {
  const closeBtn = page.getByRole('button', { name: 'Close settings' })

  if ((await closeBtn.count()) > 0) {
    await closeBtn.first().click()
  } else {
    await page.keyboard.press('Escape')
  }

  await expect(page.locator('[data-overlay-surface]')).toHaveCount(0, { timeout: 30_000 })
}

/** The Kanban nav renders inside the Sessions pane, which stays hidden after openBots()
 *  activated the Bots pane; bring Sessions forward so the nav enters the accessibility
 *  tree (Bots and Sessions are one enforced tab group, hermes-bots/plugin.tsx:359; the
 *  inactive pane is aria-hidden + visibility:hidden, tree-group.tsx:666,680). */
async function activateSessions(page: Page): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'sessions', exact: true })
    .or(page.getByRole('tab', { name: 'sessions', exact: true }))
    .first()

  await tab.click()
  await expect(
    page.locator('[data-tour="sessions-sidebar"]').filter({ visible: true }).first()
  ).toBeVisible({ timeout: 30_000 })
}

/** Enable the opt-in Kanban plugin through the product's Settings ▸ Plugins Switch and
 *  navigate to its board. The board-response listener is armed BEFORE the enable click:
 *  enabling mounts the sidebar KanbanCount, which issues the single boardKey(slug,false)
 *  GET the query client then caches for 60s (query-client.ts:10), so a listener armed
 *  only around the nav click could miss the one real request and hang. */
async function openKanbanBoard(page: Page): Promise<Response> {
  await page.evaluate(() => {
    window.location.hash = '/settings?tab=plugins'
  })
  const enableKanban = page.getByRole('switch', { name: 'Enable Kanban' })
  await expect(enableKanban).toBeVisible({ timeout: 30_000 })

  const boardResponsePromise = page.waitForResponse(
    response =>
      response.request().method() === 'GET' &&
      /\/api\/plugins\/kanban\/board(?:\?|$)/.test(response.url()),
    { timeout: 60_000 }
  )

  await enableKanban.click()
  // The nav registers reactively once the switch reads "Disable Kanban" (no reload).
  await expect(page.getByRole('switch', { name: 'Disable Kanban' })).toBeVisible({ timeout: 30_000 })
  await dismissSettingsOverlay(page)
  await activateSessions(page)
  const kanbanNav = page.getByRole('button', { name: 'Kanban', exact: true })
  await expect(kanbanNav).toBeVisible({ timeout: 30_000 })
  await kanbanNav.click()
  const boardResponse = await boardResponsePromise
  await expect(page.getByRole('heading', { name: 'Kanban', level: 1 })).toBeVisible({ timeout: 30_000 })

  return boardResponse
}

/** Drive the full counted-order FLEET-F62 desktop navigation, taking one screenshot per
 *  step and invoking the optional per-step checkpoint after each action completes. The
 *  driver's own locators/waits are the mechanical proof each action completed; the
 *  checkpoints carry any counted assertions. */
export async function driveFleetF62Nav(
  page: Page,
  checkpoints: FleetF62NavCheckpoints = {}
): Promise<void> {
  await openBots(page)
  await page.screenshot({ path: test.info().outputPath('step-1-bots-roster.png') })
  await checkpoints.afterBotsRoster?.(page)

  await createRoom(page, 'fleet-room', COWORKERS.map(b => b.title))
  await createRoom(page, 'review-room', REVIEW_ROOM_TITLES)
  await page.screenshot({ path: test.info().outputPath('step-2-standing-rooms.png') })
  await checkpoints.afterRooms?.(page)

  await openOrchestratorChat(page)
  await page.screenshot({ path: test.info().outputPath('step-3-orchestrator-bot-chat.png') })
  await checkpoints.afterOrchestratorChat?.(page)

  const boardResponse = await openKanbanBoard(page)
  await page.screenshot({ path: test.info().outputPath('step-4-kanban-board.png') })
  await checkpoints.afterKanbanBoard?.(page, boardResponse)
}
