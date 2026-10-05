import { beforeEach, describe, expect, it, vi } from 'vitest'

// launchDesktop must not orphan an Electron process when the app launches but never
// yields a usable first window (firstWindow / installErrorBannerGuard throwing).

// vi.hoisted so the mock fn exists before the hoisted vi.mock factory references it.
const launch = vi.hoisted(() => vi.fn())
const appClose = vi.fn(async () => undefined)

vi.mock('@playwright/test', () => ({ _electron: { launch } }))
vi.mock('./electron-binary', () => ({ resolveElectronBinary: () => '/fake/electron' }))
vi.mock('./test', () => ({ installErrorBannerGuard: vi.fn() }))
vi.mock('./mock-server', () => ({ startMockServer: vi.fn() }))
vi.mock('node:fs', () => ({
  existsSync: () => true,
  mkdirSync: vi.fn(),
  writeFileSync: vi.fn(),
  rmSync: vi.fn(),
  mkdtempSync: vi.fn(() => '/tmp/fleet-f62-fixtures-unit')
}))

import { launchDesktop } from './fixtures'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('launchDesktop fail-closed', () => {
  it('closes the Electron app when firstWindow rejects after launch', async () => {
    launch.mockResolvedValueOnce({
      firstWindow: vi.fn().mockRejectedValue(new Error('no first window')),
      close: appClose
    })

    await expect(launchDesktop({})).rejects.toThrow('no first window')
    expect(appClose).toHaveBeenCalledTimes(1)
  })

  it('returns the app and page on a healthy launch without closing', async () => {
    const page = { id: 'page' }
    launch.mockResolvedValueOnce({
      firstWindow: vi.fn().mockResolvedValue(page),
      close: appClose
    })

    const result = await launchDesktop({})
    expect(result.app.close).toBe(appClose)
    expect(result.page).toBe(page)
    expect(appClose).not.toHaveBeenCalled()
  })
})
