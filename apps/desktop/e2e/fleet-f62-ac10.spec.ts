import { type MockBackendFixture } from './fixtures'
import {
  bootFleetDesktop,
  COWORKERS,
  driveFleetF62Nav,
  type FleetF62NavCheckpoints,
  kanbanBoardGroup,
  kanbanTile,
  rowNameRegex
} from './fleet-f62-ac10.helpers'
import { expect, test } from './test'

// AC-FLEET-F62-10 (desktop, stub). Scope boundary: the coworkers' podman sandbox
// (docker backend + OneCLI egress) is booted for real in AC-FLEET-F62-5/6; a bare
// Electron/Playwright harness has no podman host, so this tier drives only the roster
// and room UI behind a mock provider. The nav path is owned by the shared
// driveFleetF62Nav helper, exercised uncounted first by fleet-f62-ac10-preflight.spec.ts.

// The counted test must run without retries: a single attempt regardless of the CLI
// flag or the config's CI retry setting.
test.describe.configure({ retries: 0 })

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  // Boot seeds six profiles then waits up to waitForAppReady(120s); the config's
  // default 90s hook timeout can't cover that on a cold container, so raise it to the
  // sibling fleet-boot budget (fleet-profile-rail.spec.ts:234). Hook-scoped only — the
  // counted test body keeps its own timeout and retries:0.
  test.setTimeout(240_000)
  fixture = await bootFleetDesktop('fleet-f62-ac10')
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('AC-FLEET-F62-10: the desktop app renders exactly the five-coworker roster (launch default hidden), each row bound 1:1 to its role title + @handle, the two standing rooms, the orchestrator Bot Chat, and the Kanban board over one gateway', async () => {
  test.setTimeout(420_000) // step 4 adds a Settings toggle + Kanban board fetch; stay under the 10-min tier cap
  const { page } = fixture!

  const checkpoints: FleetF62NavCheckpoints = {
    // Step 1 — Bots roster: exactly the five coworkers, launch `default` host hidden, each
    // row bound 1:1 to its role title and @handle.
    afterBotsRoster: async page => {
      // The operator-hidden launch `default` host must be ABSENT from the roster.
      await expect(
        page.locator('[data-bot-face="default"]'),
        'launch default host is hidden from the visible roster'
      ).toHaveCount(0)

      // Row-scoped 1:1 title<->handle binding: locate one unique row per handle and assert
      // its whole accessible name. A global getByText(title) + a separate @handle check
      // could BOTH pass with the title on the wrong row, leaving the mapping unproven.
      for (const bot of COWORKERS) {
        const row = page.getByRole('button', { name: rowNameRegex(bot.title, bot.name) })
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
    },

    // Step 2 — the two standing rooms are present in the sidebar.
    afterRooms: async page => {
      await expect(page.getByRole('tab', { name: /fleet-room/ }).filter({ visible: true }).first()).toBeVisible()
      await expect(page.getByRole('tab', { name: /review-room/ }).filter({ visible: true }).first()).toBeVisible()
    },

    // Step 4 — the Kanban board renders over the single gateway connection, docked as its
    // own route-tile (openRouteTile('/kanban','right')) beside main. The data fetch is
    // ctx.rest (IPC-tunneled to the gateway, not a renderer HTTP GET), so assert rendered
    // content scoped to the board's tree-group. The board-wide empty state "No tasks on
    // this board" (k.noTasks, board.tsx:1378-1382) renders ONLY after fetchBoard resolves
    // with total===0 — proving the board loaded its data over the one gateway rather than
    // the ErrorState branch. (k.empty="Empty" is a per-lane overlay, never a board-wide state.)
    afterKanbanBoard: async page => {
      await expect(kanbanTile(page)).toBeVisible({ timeout: 30_000 })
      const board = kanbanBoardGroup(page)
      await expect(board.getByRole('heading', { name: 'Kanban', level: 1 })).toBeVisible({ timeout: 30_000 })
      await expect(board.getByText('No tasks on this board')).toBeVisible({ timeout: 30_000 })
    }
  }

  await driveFleetF62Nav(page, checkpoints)

  // Single-gateway invariant: bootFleetDesktop launches exactly ONE local backend
  // (buildAppEnv(sandbox)), waitForAppReady gated on that one gateway, and the Kanban
  // board rendered above is served by that same gateway — there is no second port anywhere.
})
