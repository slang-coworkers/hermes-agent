import { type MockBackendFixture } from './fixtures'
import { bootFleetDesktop, driveFleetF62Nav } from './fleet-f62-ac10.helpers'
import { expect, test } from './test'

// FLEET-F62 desktop navigation preflight (R5-1, UNCOUNTED — NOT an AC id).
//
// Purpose: prove the full shared driveFleetF62Nav path completes MECHANICALLY on this
// head before the counted AC-FLEET-F62-10 runs, so a new nav bug cannot first surface
// mid-counted-run and burn the final round. It drives the SAME driveFleetF62Nav helper
// as fleet-f62-ac10.spec.ts (they cannot drift) but passes mechanical-only checkpoints
// and holds NONE of the counted criteria (exact-five roster, the title↔handle regex, the
// rooms' membership, the board status, one gateway stay ONLY in the counted spec).
//
// State isolation (R5-1): bootFleetDesktop('fleet-f62-ac10-preflight') creates a FRESH
// disposable sandbox root distinct from the counted spec's, holding both HERMES_HOME and
// HERMES_DESKTOP_USER_DATA_DIR; afterAll closes the app + gateway and destroys the whole
// root, so no room DB, plugin-decision/localStorage or connection carries into the
// counted AC-10.
//
// Operational classification (R5-3, fail-closed — the tester's rubric, mirrored here so
// the intent is on record): a preflight failure is an uncounted in-surface
// DRIVER-PREFLIGHT FAIL (fixed in apps/desktop/e2e/** and re-run) ONLY when its root cause
// is a PROVEN test-driver defect — a FIXTURE failure proven by pre-launch fs/schema
// validation BEFORE product ingestion, or a SELECTOR/ACTIONABILITY failure proven by the
// trace/screenshot PLUS an independent DOM/API probe that the correct product state
// exists. ANY failure whose root cause is PRODUCT (an element genuinely absent because a
// plugin failed to register, the board endpoint 500s, an incorrect product-rendered
// state) AND any failure whose root cause is INCONCLUSIVE (that proof absent) is recorded
// IMMEDIATELY as a COUNTED AC-FLEET-F62-10 failure. The default when unproven is COUNTED;
// this preflight never reclassifies a product defect.
//
// This spec does NOT force zero retries: it is a mechanical gate, so a retry is
// acceptable. The counted spec is the one pinned to retries: 0 (R5-5).

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
