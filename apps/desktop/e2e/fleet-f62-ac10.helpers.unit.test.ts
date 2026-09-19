import { beforeEach, describe, expect, it, vi } from 'vitest'

// Fault-injection coverage for bootFleetDesktop's fail-closed contract: a failure at any
// start step must leak no mock server, Electron process, or sandbox root, and cleanup
// must destroy the sandbox root even if closing the app or mock rejects.

const mockClose = vi.fn(async () => undefined)
const sandboxCleanup = vi.fn()
const appClose = vi.fn(async () => undefined)

vi.mock('./mock-server', () => ({
  startMockServer: vi.fn(async () => ({ url: 'http://127.0.0.1:0', close: mockClose }))
}))

vi.mock('./fixtures', () => ({
  createSandbox: vi.fn(() => ({
    root: '/tmp/fleet-f62-unit',
    hermesHome: '/tmp/fleet-f62-unit/hermes-home',
    userDataDir: '/tmp/fleet-f62-unit/electron-user-data',
    cleanup: sandboxCleanup
  })),
  buildAppEnv: vi.fn(() => ({})),
  launchDesktop: vi.fn(async () => ({ app: { close: appClose }, page: {} })),
  waitForAppReady: vi.fn(async () => undefined),
  writeMockProviderConfig: vi.fn(),
  writeEnvFile: vi.fn()
}))

vi.mock('./real-session-builder', () => ({
  RealSessionBuilder: { start: vi.fn(async () => ({ createSession: vi.fn(async () => undefined), close: vi.fn(async () => undefined) })) }
}))

// The helper touches only these fs entry points during seeding; keep the unit test off disk.
vi.mock('node:fs', () => ({
  default: { mkdirSync: vi.fn(), writeFileSync: vi.fn(), existsSync: () => false, readFileSync: vi.fn() }
}))

// driveFleetF62Nav imports Playwright's test/expect at module load; bootFleetDesktop uses
// neither, so a lightweight stub keeps the module importable in the node runner.
vi.mock('./test', () => ({
  test: { info: () => ({ outputPath: (name: string) => name }) },
  expect: () => ({ toBeVisible: async () => undefined, toHaveCount: async () => undefined })
}))

import { createSandbox, launchDesktop, waitForAppReady } from './fixtures'
import { bootFleetDesktop } from './fleet-f62-ac10.helpers'
import { startMockServer } from './mock-server'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('bootFleetDesktop fail-closed contract', () => {
  it('closes the mock server when sandbox creation fails', async () => {
    vi.mocked(createSandbox).mockImplementationOnce(() => {
      throw new Error('createSandbox boom')
    })

    await expect(bootFleetDesktop('p')).rejects.toThrow('createSandbox boom')

    expect(mockClose).toHaveBeenCalledTimes(1)
    expect(appClose).not.toHaveBeenCalled()
    expect(sandboxCleanup).not.toHaveBeenCalled()
  })

  it('closes the mock and destroys the sandbox when the app fails to launch', async () => {
    vi.mocked(launchDesktop).mockRejectedValueOnce(new Error('launch boom'))

    await expect(bootFleetDesktop('p')).rejects.toThrow('launch boom')

    expect(mockClose).toHaveBeenCalledTimes(1)
    expect(sandboxCleanup).toHaveBeenCalledTimes(1)
    expect(appClose).not.toHaveBeenCalled()
  })

  it('closes the app, the mock, and destroys the sandbox when readiness fails', async () => {
    vi.mocked(waitForAppReady).mockRejectedValueOnce(new Error('ready boom'))

    await expect(bootFleetDesktop('p')).rejects.toThrow('ready boom')

    expect(appClose).toHaveBeenCalledTimes(1)
    expect(mockClose).toHaveBeenCalledTimes(1)
    expect(sandboxCleanup).toHaveBeenCalledTimes(1)
  })

  it('returns a fixture whose cleanup destroys the sandbox even if closing the mock rejects', async () => {
    const fixture = await bootFleetDesktop('p')

    expect(startMockServer).toHaveBeenCalledTimes(1)
    expect(appClose).not.toHaveBeenCalled()
    expect(sandboxCleanup).not.toHaveBeenCalled()

    mockClose.mockRejectedValueOnce(new Error('close boom'))
    await fixture.cleanup()

    expect(appClose).toHaveBeenCalledTimes(1)
    expect(mockClose).toHaveBeenCalledTimes(1)
    expect(sandboxCleanup).toHaveBeenCalledTimes(1)
  })
})
