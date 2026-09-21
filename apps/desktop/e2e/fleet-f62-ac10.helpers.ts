import fs from 'node:fs'
import path from 'node:path'

import type { ElectronApplication } from '@playwright/test'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type MockBackendFixture,
  REPO_ROOT,
  type Sandbox,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { startMockServer } from './mock-server'
import { RealSessionBuilder } from './real-session-builder'
import { expect, test } from './test'

// Shared FLEET-F62 desktop driver: the counted spec and the uncounted preflight both
// call driveFleetF62Nav, so the nav path they exercise cannot drift apart. The driver
// owns the nav actions and their sequencing waits; callers add any assertions via the
// checkpoints.

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
  afterKanbanBoard?: (page: Page) => Promise<void>
}

// ─── Kanban board locators (shared by the driver + both specs' checkpoints) ──

// The route-tile's tab: `openRouteTile('/kanban','right')` docks a pane whose id
// is `route-tile:/kanban` (pane-mirror.ts `prefix:'route-tile'` + `key:t=>t.path`),
// carried as `data-tree-tab` on the tab element (tree-group.tsx:469,541).
export const kanbanTile = (page: Page) => page.locator('[data-tree-tab="route-tile:/kanban"]')

// The leaf tree-group holding that tab; the same group renders the board pane
// body, so board content scopes to it. Only tree-group.tsx emits
// `data-tree-group` (tree-split.tsx renders split panels, not groups), so
// `.filter({has: tile})` resolves the one board group. Hidden pane layers carry
// `data-pane-hidden` with `visibility:hidden` (pane-visibility.ts:18), which
// `toBeVisible()` already excludes — no explicit `:not([data-pane-hidden])` needed.
export const kanbanBoardGroup = (page: Page) =>
  page.locator('[data-tree-group]').filter({ has: kanbanTile(page) }).first()

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
 *  and cleanup() destroys the whole root — then launch a NEW Electron + gateway. The
 *  preflight and the counted spec pass DISTINCT prefixes, so neither the room DB nor the
 *  desktop plugin-decision store carries between them. Fail-closed: a partial-start
 *  exception closes the app + mock and destroys the root before it rethrows, so a boot
 *  failure leaks no resources or half-seeded root. */
export async function bootFleetDesktop(prefix: string): Promise<MockBackendFixture> {
  let mock: Awaited<ReturnType<typeof startMockServer>> | undefined
  let sandbox: Sandbox | undefined
  let app: ElectronApplication | undefined

  try {
    mock = await startMockServer()
    sandbox = createSandbox(prefix)
    // The DEFAULT (launch) profile is the multiplexer host; the five coworkers are the
    // served secondaries. All six live in ONE HERMES_HOME behind ONE gateway.
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)

    for (const bot of COWORKERS) {
      await seedCoworker(sandbox.hermesHome, mock.url, bot)
    }

    seedDefaultHidden(sandbox.hermesHome)

    // C2 (FLEET-F62): pin bundled-plugin discovery to the tested worktree's plugins dir
    // so the spawned headless `hermes serve` DISCOVERS kanban and mounts
    // /api/plugins/kanban/board (web_server.py honours HERMES_BUNDLED_PLUGINS via
    // get_bundled_plugins_dir; bundled plugins mount regardless of plugins.enabled).
    // Scoped to this AC-10 harness (preflight + counted both boot through here).
    const launched = await launchDesktop(
      buildAppEnv(sandbox, { HERMES_BUNDLED_PLUGINS: path.join(REPO_ROOT, 'plugins') })
    )
    app = launched.app

    const boundMock = mock
    const boundSandbox = sandbox

    const fixture: MockBackendFixture = {
      app: launched.app,
      page: launched.page,
      mock: boundMock,
      mockUrl: boundMock.url,
      sandbox: boundSandbox,
      cleanup: async () => {
        // Destroy the sandbox root even if closing the app or mock rejects.
        try {
          await launched.app.close().catch(() => undefined)
          await boundMock.close().catch(() => undefined)
        } finally {
          boundSandbox.cleanup()
        }
      }
    }

    await waitForAppReady(fixture, 120_000)

    return fixture
  } catch (err) {
    // Close only what was actually started, so a failure at any point leaks no Electron
    // process, mock server, or sandbox root.
    if (app) {await app.close().catch(() => undefined)}

    if (mock) {await mock.close().catch(() => undefined)}

    if (sandbox) {sandbox.cleanup()}
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

/** Dismiss the full-viewport Settings OverlayView (data-overlay-surface,
 *  overlay-view.tsx:74-104) before touching the Sessions tab: while open it intercepts
 *  pointer events on the panes beneath it, so a Sessions-tab click would be intercepted.
 *  Prefer the labeled Close-settings control (overlay-view.tsx:126-134), fall back to
 *  Escape (overlay-view.tsx:53-72), then await the surface's removal. */
async function dismissSettingsOverlay(page: Page): Promise<void> {
  const closeBtn = page.getByRole('button', { name: 'Close settings' })

  if ((await closeBtn.count()) > 0) {
    await closeBtn.first().click()
  } else {
    await page.keyboard.press('Escape')
  }

  await expect(page.locator('[data-overlay-surface]')).toHaveCount(0, { timeout: 30_000 })
}

/** The Kanban sidebar nav renders inside the Sessions pane, which stays hidden after
 *  openBots() activated the Bots pane; bring Sessions forward so the nav row enters the
 *  DOM (Bots and Sessions are one enforced tab group; the inactive pane is aria-hidden +
 *  visibility:hidden, tree-group.tsx). */
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

/** Enable the opt-in Kanban plugin through the product's Settings ▸ Plugins Switch,
 *  dismiss the Settings overlay, bring the Sessions pane forward, then dock the board
 *  via the split affordance. */
async function openKanbanBoard(page: Page): Promise<void> {
  await page.evaluate(() => {
    window.location.hash = '/settings?tab=plugins'
  })
  const enableKanban = page.getByRole('switch', { name: 'Enable Kanban' })
  await expect(enableKanban).toBeVisible({ timeout: 30_000 })
  await enableKanban.click()
  // The switch flipping to "Disable Kanban" confirms the plugin is enabled, so its
  // /kanban route, board, and sidebar nav row are now contributed.
  await expect(page.getByRole('switch', { name: 'Disable Kanban' })).toBeVisible({ timeout: 30_000 })
  await dismissSettingsOverlay(page)
  await activateSessions(page)
  const kanbanNav = page.getByRole('button', { name: 'Kanban', exact: true })
  await expect(kanbanNav).toBeVisible({ timeout: 30_000 })
  // Surface the board via the product's split affordance, NOT the left-click nav:
  // left-click routes through navigateToWorkspacePage→revealWorkspacePane, which leaves
  // the board present-but-aria-hidden when a Bot Chat session-tile holds the main zone
  // (bug #72602, routes.ts:223-234). Right-click the nav row → context menu (aria-label
  // "Kanban") → "Open in split" → "Right" → openRouteTile('/kanban','right') docks the
  // board route-tile in its own visible split zone (sidebar/index.tsx:1556-1573,
  // split-submenu.tsx:62-91, store/route-tiles.ts:40).
  await kanbanNav.click({ button: 'right' })
  const menu = page.getByRole('menu', { name: 'Kanban' })
  await expect(menu).toBeVisible({ timeout: 30_000 })
  await menu.getByRole('menuitem', { name: 'Open in split' }).hover()
  const rightSplit = page.getByRole('menuitem', { name: 'Right', exact: true })
  await expect(rightSplit).toBeVisible({ timeout: 30_000 })
  await rightSplit.click()
  // Reachability = the route-tile docks and the board mounts. The data fetch is ctx.rest
  // over the Electron IPC bridge (NOT a renderer HTTP GET), so assert rendered content,
  // never page.waitForResponse('/api/plugins/kanban/board').
  await expect(kanbanTile(page)).toBeVisible({ timeout: 60_000 })
  await expect(kanbanBoardGroup(page).getByRole('heading', { name: 'Kanban', level: 1 })).toBeVisible({
    timeout: 60_000
  })
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

  await openKanbanBoard(page)
  await page.screenshot({ path: test.info().outputPath('step-4-kanban-board.png') })
  await checkpoints.afterKanbanBoard?.(page)
}
