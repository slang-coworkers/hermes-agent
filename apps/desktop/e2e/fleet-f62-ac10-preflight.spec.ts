import { type MockBackendFixture } from './fixtures'
import { bootFleetDesktop, driveFleetF62Nav } from './fleet-f62-ac10.helpers'
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
  fixture = await bootFleetDesktop('fleet-f62-ac10-preflight')
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('FLEET-F62 desktop navigation preflight (uncounted): the shared driveFleetF62Nav path completes mechanically end-to-end', async () => {
  test.setTimeout(420_000)
  const { page } = fixture!

  // Mechanical-only checkpoints: confirm each action reached the next view without
  // asserting a counted criterion. The driver's own locators/waits (roster rendered,
  // room tabs, Bot Chat tab, overlay dismissed, Sessions pane visible, Kanban board
  // request + heading) are the primary proof; these callbacks add the lightest
  // presence checks and never assert exact-five, the title↔handle binding or the board
  // status.
  await driveFleetF62Nav(page, {
    afterBotsRoster: async page => {
      await expect(
        page.locator('[data-bot-face]').first(),
        'at least one coworker face rendered in the roster'
      ).toBeVisible()
    },
    afterKanbanBoard: async (_page, boardResponse) => {
      expect(boardResponse, 'a Kanban board response was received').toBeTruthy()
    }
  })
})
