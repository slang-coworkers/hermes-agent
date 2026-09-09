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

// AC-SELF-F56-1 (desktop, model: stub) — per the LOOP-F35 ADR ## Amendment:
//   "After onboard coworker the desktop app Bots list renders one row per
//    onboarded coworker profile, each row displaying that profile's title
//    next to its avatar"
//
// This spec proves the roster render. The message_agent round trip belongs to
// the live tier (AC-LOOP-F35-5, real model): the desktop mock server scripts
// only E2E_* keyword turns (apps/desktop/e2e/mock-server.ts), so it cannot
// drive message_agent. The Bots row shows title + avatar but not description
// (bot-row.tsx:179-181,212), so description is confirmed from profile.yaml.
//
// The roster treats a profile as a bot once ui_meta['hermes-bots'] is present
// on its profile.yaml (tools/bot_mode_probe.py). This spec seeds that on disk
// per bot, reusing bot-mode-closed-chat-stays-closed.spec.ts:93-114 for the
// profile-dir/provider/session bootstrap and adding ui_meta['hermes-bots'].

type Page = MockBackendFixture['page']

// Mirrors tests/plugins/fixtures/loop-f35/expected.yaml `bot_meta` (FROZEN) —
// the same BotMeta values the strengthened test_ac_self_f56_3 proves the plugin
// emits, and that the desktop roster renders here.
const BOTS = [
  { name: 'reviewer', title: 'Reviewer', description: 'PR reviewer', shape: 'diamond' },
  { name: 'fixer', title: 'Fixer', description: 'Implementer', shape: 'square' },
  { name: 'orchestrator', title: 'Orchestrator', description: 'Fleet orchestrator', shape: 'hexagon' }
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

/** Seed one bot-managed coworker profile before launch: the profile dir + mock
 *  provider + a durable canonical "Bot Chat" (the :93-114 pattern), then
 *  additionally write ui_meta['hermes-bots'] into profile.yaml so the roster
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

  // Write ui_meta['hermes-bots'] directly. JSON is valid YAML, so profile.yaml
  // parses cleanly and bot_mode_probe reads the BotMeta from it. Merge onto any
  // profile.yaml the bootstrap already wrote.
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

test.beforeAll(async () => {
  const mock = await startMockServer()
  const sandbox = createSandbox('loop-f35-ac1')
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  for (const bot of BOTS) {
    await seedCoworker(sandbox.hermesHome, mock.url, bot)
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

test('AC-SELF-F56-1: Bots list renders one row per onboarded coworker profile, each showing its title next to its avatar', async () => {
  test.setTimeout(300_000)
  const { page, sandbox } = fixture!

  await openBots(page)
  await page.screenshot({ path: test.info().outputPath('step-1-bots-pane.png') })

  // Step: one row per onboarded coworker profile, located by title, avatar present.
  for (const bot of BOTS) {
    const row = page
      .getByRole('button', { name: new RegExp(`^${bot.title}\\b`, 'i') })
      .filter({ visible: true })
      .first()
    await expect(row, `row for ${bot.name} (${bot.title}) is rendered`).toBeVisible({ timeout: 30_000 })
    await expect(
      row.locator(`[data-bot-face="${bot.name}"]`),
      `avatar element for ${bot.name} is present next to its title`
    ).toBeVisible()
  }

  await page.screenshot({ path: test.info().outputPath('step-2-rows-with-avatars.png') })

  // Evidence / fixture-consumption check: description is not rendered in the row
  // (bot-row.tsx), so confirm each profile.yaml carries the seeded BotMeta
  // title/description/shape that the roster consumed.
  for (const bot of BOTS) {
    const raw = fs.readFileSync(path.join(sandbox.hermesHome, 'profiles', bot.name, 'profile.yaml'), 'utf8')
    expect(raw, `${bot.name} profile.yaml carries its BotMeta title`).toContain(bot.title)
    expect(raw, `${bot.name} profile.yaml carries its BotMeta description`).toContain(bot.description)
    expect(raw, `${bot.name} profile.yaml carries its BotMeta shape`).toContain(bot.shape)
  }
})
