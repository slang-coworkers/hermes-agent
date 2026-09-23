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
import { expect, test } from './test'

// AC-COST-F30-7 (desktop): the merged nv-cost-cap plugin's desktop half renders the
// cost-escalation card ONLY after the full package is installed into the desktop host's
// $HERMES_HOME/plugins/nv-cost-cap/ and enabled (defaultEnabled:false), over the SAME
// /api/plugins/nv-cost-cap backend the e2e harness's real `hermes serve` exposes. An
// unauthorized principal applies no mutation, an authorized operator resolves Continue once
// (repeat = no-op), Stop leaves the session blocked, set-ceiling writes the exact value and clears
// the block, and an immortal (daily) card offers only Continue. The backend runs loopback, so the
// DEFAULT profile opts into the explicit loopback_operator (the single trusted local token
// authorizes as the operator); the LOCKED profile does not, so a resolve scoped there is refused —
// the authz boundary. estop_on_breach=false, so these crossings block per-session with no belt.

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..')
const PLUGIN_SRC = path.join(REPO_ROOT, 'plugins', 'nv-cost-cap')
const STORE_PY = path.join(PLUGIN_SRC, 'store.py')
const LOOPBACK_OP = 'dashboard:oauth:org-1:op-1'
const INCREMENT = 0.05

let fixture: MockBackendFixture | null = null
let defaultEp = ''
let lockedEp = ''

// Seed / read a profile's data.db through the plugin's OWN store module (no schema
// duplication). The image has no sqlite3 CLI; python3 + the store is the sanctioned form.
function runStore(profileHome: string, snippet: string): string {
  const script = `
import os, sys, importlib.util
os.environ['HERMES_HOME'] = ${JSON.stringify(profileHome)}
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
spec = importlib.util.spec_from_file_location('nvcc_store', ${JSON.stringify(STORE_PY)})
store = importlib.util.module_from_spec(spec); sys.modules['nvcc_store'] = store; spec.loader.exec_module(store)
${snippet}`
  return execFileSync('python3', ['-c', script], { encoding: 'utf8' }).trim()
}

function seedCrossing(profileHome: string, sessionId: string, opts: { immortal?: boolean } = {}): string {
  const immortal = opts.immortal ? 1 : 0
  const kind = opts.immortal ? 'daily' : 'breach'
  return runStore(profileHome, `
store.set_state(${JSON.stringify(sessionId)}, effective_usd=1.0, window_start_total=0.0, day_start_total=0.0, budget_gen=1, blocked=1, immortal=${immortal})
print(store.record_episode(${JSON.stringify(sessionId)}, ${JSON.stringify(kind)}, 1, '2026-09-19', dedup_key=${JSON.stringify(sessionId + ':' + kind + ':1')}))`)
}

function appliedCount(profileHome: string, sessionId: string): number {
  return Number.parseInt(
    runStore(profileHome, `print(len([r for r in store.resolutions(${JSON.stringify(sessionId)}) if r['status'] == 'applied']))`),
    10
  )
}

function appliedDecisionCount(profileHome: string, sessionId: string, decision: string): number {
  return Number.parseInt(
    runStore(profileHome, `print(len([r for r in store.resolutions(${JSON.stringify(sessionId)}) if r['status'] == 'applied' and r['decision'] == ${JSON.stringify(decision)}]))`),
    10
  )
}

function isBlocked(profileHome: string, sessionId: string): number {
  return Number.parseInt(
    runStore(profileHome, `print(int(bool(dict(store.get_state(${JSON.stringify(sessionId)})).get('blocked'))))`),
    10
  )
}

function resolvedCeiling(profileHome: string): number {
  return Number.parseFloat(runStore(profileHome, `print(store.resolved_ceiling())`))
}

async function apiFetch(page: MockBackendFixture['page'], method: string, url: string, body?: unknown) {
  // The packaged renderer is file-backed, so issue HTTP from the Node test process to avoid CORS.
  const conn = await page.evaluate(() =>
    (window as unknown as {
      hermesDesktop: { getConnection: (profile: string) => Promise<{ baseUrl: string; token: string }> }
    }).hermesDesktop.getConnection('default')
  )
  const headers: Record<string, string> = { 'X-Hermes-Session-Token': conn.token }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(conn.baseUrl + url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  return { status: response.status, body: await response.json().catch(() => null) }
}

test.beforeAll(async () => {
  const mock = await startMockServer()
  const sandbox = createSandbox('cost-f30-ac7')

  fs.cpSync(PLUGIN_SRC, path.join(sandbox.hermesHome, 'plugins', 'nv-cost-cap'), { recursive: true })

  const extraConfig = `plugins:
  enabled:
    - nv-cost-cap
  entries:
    nv-cost-cap:
      settings:
        operators:
          - "${LOOPBACK_OP}"
        loopback_operator: "${LOOPBACK_OP}"
        default_ceiling_usd: 0.02
        escalation_increment_usd: ${INCREMENT}
        estop_on_breach: false`
  writeMockProviderConfig(sandbox.hermesHome, mock.url, undefined, extraConfig)
  writeEnvFile(sandbox.hermesHome)

  // DEFAULT profile: authorized (loopback_operator set). LOCKED profile: an operator is
  // listed but loopback_operator is NOT, so a loopback resolve there yields a None principal.
  defaultEp = seedCrossing(sandbox.hermesHome, 'sess-cost-f30-ac7')
  const lockedHome = path.join(sandbox.hermesHome, 'profiles', 'cost-f30-locked')
  fs.mkdirSync(lockedHome, { recursive: true })
  fs.writeFileSync(path.join(lockedHome, 'config.yaml'),
    `plugins:\n  enabled:\n    - nv-cost-cap\n  entries:\n    nv-cost-cap:\n      settings:\n        operators:\n          - "${LOOPBACK_OP}"\n`, 'utf8')
  lockedEp = seedCrossing(lockedHome, 'sess-cost-f30-ac7-locked')

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))
  fixture = {
    app, page, mock, mockUrl: mock.url, sandbox,
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

test('AC-COST-F30-7: the desktop escalation card renders after install+enable; Continue/Stop/set-ceiling resolve and an immortal card is Continue-only', async () => {
  test.setTimeout(600_000)
  const { page, sandbox } = fixture!
  const home = sandbox.hermesHome
  const lockedHome = path.join(home, 'profiles', 'cost-f30-locked')

  // Enable the desktop half (ships default-off) via the persisted decision store, then reload.
  await page.evaluate((id) => {
    window.localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ [id]: true }))
  }, 'nv-cost-cap')
  await page.reload()
  await waitForAppReady(fixture!, 120_000)
  await page.screenshot({ path: test.info().outputPath('step-1-loaded.png') })

  // Backend inventory reports the plugin serving escalations for the active (default) profile.
  const listed = await apiFetch(page, 'GET', '/api/plugins/nv-cost-cap/escalations?profile=default')
  expect(listed.status, `GET escalations OK — ${JSON.stringify(listed)}`).toBeLessThan(400)
  expect(Array.isArray(listed.body) && listed.body.some((e: { session?: string }) => e.session === 'sess-cost-f30-ac7'),
    'the seeded crossing is listed for the active profile').toBeTruthy()

  // The pane adopts into the collapsed right sidebar (no named tab), so the operator-visible
  // reveal is the titlebar "Show right sidebar" control.
  const showSidebar = page.getByRole('button', { name: /show right sidebar/i }).first()
  await expect(showSidebar,
    'the "Show right sidebar" control is present (operator-visible activation)').toBeVisible({ timeout: 30_000 })
  await showSidebar.click()

  // Match the card's own "Cost escalation — session <id>" text so the assertion binds to the
  // revealed content, not to any control.
  await expect(page.getByText(/cost escalation.*session/i).first(),
    'the escalation card is visible after opening the right sidebar').toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: test.info().outputPath('step-2-card.png') })

  // An unauthorized principal (locked profile: no loopback_operator -> None) applies NOTHING.
  const denied = await apiFetch(page, 'POST', `/api/plugins/nv-cost-cap/escalations/${lockedEp}/resolve?profile=cost-f30-locked`, { decision: 'continue' })
  expect(
    (denied.status === 403 && denied.body?.detail === 'unauthorized') ||
    (denied.status < 400 && denied.body?.granted === false && denied.body?.reason === 'unauthorized'),
    `unauthorized resolve refused — ${JSON.stringify(denied)}`
  ).toBeTruthy()
  expect(appliedCount(lockedHome, 'sess-cost-f30-ac7-locked'), 'no mutation for the unauthorized principal').toBe(0)

  // The authorized operator resolves once (Continue via the card).
  expect(appliedCount(home, 'sess-cost-f30-ac7'), 'no resolution applied yet').toBe(0)
  await page.getByRole('button', { name: /^continue$/i }).first().click()
  await expect.poll(() => appliedCount(home, 'sess-cost-f30-ac7'), { timeout: 30_000 }).toBe(1)
  await page.screenshot({ path: test.info().outputPath('step-3-resolved.png') })

  // A repeat resolution of the same episode is a reported no-op.
  const repeat = await apiFetch(page, 'POST', `/api/plugins/nv-cost-cap/escalations/${defaultEp}/resolve?profile=default`, { decision: 'continue' })
  expect(repeat.body?.granted === false && repeat.body?.reason === 'already-resolved',
    `repeat is a reported no-op — ${JSON.stringify(repeat)}`).toBeTruthy()
  expect(appliedCount(home, 'sess-cost-f30-ac7'), 'still exactly one increment after the repeat').toBe(1)

  // Stop blocks the session with no resume. A fresh crossing surfaces its own card; the earlier
  // (resolved) episodes are now note-only rows with no buttons, so the Stop control is unambiguous.
  seedCrossing(home, 'sess-cost-f30-ac7-stop')
  await expect(page.getByText(/session sess-cost-f30-ac7-stop/i).first(),
    'the fresh Stop crossing appears as a card').toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: /^stop$/i }).first().click()
  await expect.poll(() => appliedDecisionCount(home, 'sess-cost-f30-ac7-stop', 'stop'), { timeout: 30_000 }).toBe(1)
  expect(isBlocked(home, 'sess-cost-f30-ac7-stop'), 'Stop leaves the session blocked (no resume)').toBe(1)
  await page.screenshot({ path: test.info().outputPath('step-5-stop.png') })

  // Set-ceiling writes the exact value and clears the block (12.50 > spend 1.0).
  seedCrossing(home, 'sess-cost-f30-ac7-ceiling')
  await expect(page.getByText(/session sess-cost-f30-ac7-ceiling/i).first(),
    'the fresh set-ceiling crossing appears as a card').toBeVisible({ timeout: 30_000 })
  await page.getByLabel(/exact ceiling usd/i).first().fill('12.50')
  await page.getByRole('button', { name: /set ceiling/i }).first().click()
  await expect.poll(() => appliedDecisionCount(home, 'sess-cost-f30-ac7-ceiling', 'ceiling'), { timeout: 30_000 }).toBe(1)
  expect(resolvedCeiling(home), 'the profile ceiling is written exactly').toBeCloseTo(12.5, 2)
  expect(isBlocked(home, 'sess-cost-f30-ac7-ceiling'), 'a ceiling above spend clears the money block').toBe(0)
  await page.screenshot({ path: test.info().outputPath('step-6-ceiling.png') })

  // An immortal (daily) card exposes ONLY Continue — no Stop, no set-ceiling — and a set-ceiling
  // attempt is refused immortal-continue-only. By now every mortal card is resolved (note-only rows,
  // no buttons), so the only action controls belong to the immortal card.
  const immortalEp = seedCrossing(home, 'sess-cost-f30-ac7-immortal', { immortal: true })
  await expect(page.getByText(/session sess-cost-f30-ac7-immortal/i).first(),
    'the immortal crossing appears as a card').toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: /set ceiling/i }),
    'the immortal card exposes NO set-ceiling control (and prior mortal cards have cleared)').toHaveCount(0)
  await expect(page.getByRole('button', { name: /^stop$/i }),
    'the immortal card exposes NO Stop control').toHaveCount(0)
  await expect(page.getByRole('button', { name: /^continue$/i }),
    'the immortal card exposes ONLY Continue').toHaveCount(1)
  const immortalCeiling = await apiFetch(page, 'POST', `/api/plugins/nv-cost-cap/escalations/${immortalEp}/resolve?profile=default`, { decision: 'ceiling', amount_usd: 5.0 })
  expect(immortalCeiling.body?.granted === false && immortalCeiling.body?.reason === 'immortal-continue-only',
    `a set-ceiling on an immortal session is refused — ${JSON.stringify(immortalCeiling)}`).toBeTruthy()
  await page.screenshot({ path: test.info().outputPath('step-7-immortal.png') })
})
