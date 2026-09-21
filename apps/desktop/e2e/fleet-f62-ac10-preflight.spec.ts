import { type MockBackendFixture } from './fixtures'
import { bootFleetDesktop, driveFleetF62Nav, kanbanBoardGroup, kanbanTile } from './fleet-f62-ac10.helpers'
import { expect, test } from './test'

// FLEET-F62 desktop navigation preflight (uncounted — NOT an AC id).
//
// Drives the SAME driveFleetF62Nav path as the counted fleet-f62-ac10.spec.ts (they
// cannot drift) with mechanical-only checkpoints, so a nav bug surfaces here rather than
// mid-counted-run. It holds NONE of the counted criteria. State isolation:
// bootFleetDesktop('fleet-f62-ac10-preflight') uses a fresh disposable sandbox root
// distinct from the counted spec's (both HERMES_HOME and HERMES_DESKTOP_USER_DATA_DIR),
// destroyed in afterAll, so nothing carries into the counted AC-10.
//
// The fail-closed classification for a preflight failure (proven driver defect =>
// uncounted; PRODUCT or inconclusive => counted AC-FLEET-F62-10) lives in the ADR
// AC-10 §Setup and the tester hand-off. Unlike the counted spec this one does not pin
// retries: 0 — it is a mechanical gate and may retry.

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  // Boot seeds six profiles then waits up to waitForAppReady(120s); the config's
  // default 90s hook timeout can't cover that on a cold container, so raise it to the
  // sibling fleet-boot budget (fleet-profile-rail.spec.ts:234).
  test.setTimeout(240_000)
  fixture = await bootFleetDesktop('fleet-f62-ac10-preflight')
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('FLEET-F62 desktop navigation preflight (uncounted): the shared driveFleetF62Nav path completes mechanically end-to-end', async () => {
  test.setTimeout(420_000)
  const { page } = fixture!

  // C2 readiness gate (uncounted): before driving nav, prove the single gateway
  // DISCOVERED and MOUNTED kanban — GET /api/dashboard/plugins lists it and the board
  // endpoint returns a payload. Discovery alone is insufficient (a mount import failure
  // is caught+logged), so also fetch the board. Both go through the Electron IPC bridge
  // (window.hermesDesktop.api), not a renderer HTTP GET. A discovered-but-unmounted
  // kanban FAILs the preflight uncounted, before the sole counted AC-10 run.
  const readiness = await page.evaluate(async () => {
    const api = (window as unknown as {
      hermesDesktop: { api: <T>(request: { path: string }) => Promise<T> }
    }).hermesDesktop.api
    const plugins = await api<Array<{ name?: string }>>({ path: '/api/dashboard/plugins' })
    const board = await api<{ columns?: unknown[] }>({ path: '/api/plugins/kanban/board' })
    return { pluginNames: plugins.map(plugin => plugin.name), board }
  })
  expect(readiness.pluginNames, 'the single gateway discovered the bundled kanban plugin').toContain('kanban')
  expect(readiness.board, 'the kanban board API mounted and returns a columns payload').toMatchObject({
    columns: expect.any(Array)
  })

  // Mechanical-only checkpoints: confirm each action reached the next view without
  // asserting a counted criterion. The driver's own locators/waits (roster rendered,
  // room tabs, Bot Chat tab, overlay dismissed, Sessions pane visible, docked Kanban
  // route-tile + board heading) are the primary proof; these callbacks add the lightest
  // presence checks and never assert exact-five, the title↔handle binding or the board
  // status.
  await driveFleetF62Nav(page, {
    afterBotsRoster: async page => {
      await expect(
        page.locator('[data-bot-face]').first(),
        'at least one coworker face rendered in the roster'
      ).toBeVisible()
    },
    afterKanbanBoard: async page => {
      await expect(kanbanTile(page), 'the Kanban board route-tile docked beside main').toBeVisible()
      await expect(
        kanbanBoardGroup(page).getByRole('heading', { name: 'Kanban', level: 1 }),
        'the Kanban board view mounted'
      ).toBeVisible()
    }
  })
})
