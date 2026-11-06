import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { api } from '../api/client'
import { DataHomeOwnerStatusNotice } from './DataHomeOwnerNotice'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, status: vi.fn() } }
})

const FIX = "sudo chown -R ann '/srv/crew-data' && chmod -R u+rwX '/srv/crew-data'"

describe('DataHomeOwnerStatusNotice', () => {
  afterEach(() => {
    vi.mocked(api.status).mockReset()
    delete window.dataHomeFixAPI
  })

  it('shows the sentence and the command to copy when another account owns the files', async () => {
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/srv/crew-data', foreign: ['/srv/crew-data/config.json'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    const notice = await screen.findByTestId('settings-data-home-owner')
    expect(notice).toHaveTextContent("Some Kiro Crew files belong to another user, so Kiro Crew can't read them.")
    expect(notice.querySelector('code')?.textContent).toBe(FIX)
  })

  it('offers no button without the desktop bridge, so the command is the way', async () => {
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    await screen.findByTestId('settings-data-home-owner')
    expect(screen.queryByTestId('data-home-fix-button')).toBeNull()
  })

  it('offers no button where the shell says the repair cannot run here', async () => {
    window.dataHomeFixAPI = { available: vi.fn(async () => false), fix: vi.fn() }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    await screen.findByTestId('settings-data-home-owner')
    await waitFor(() => expect(window.dataHomeFixAPI!.available).toHaveBeenCalled())
    expect(screen.queryByTestId('data-home-fix-button')).toBeNull()
  })

  it('a fixed repair says so and re-reads the status, which then hides the notice', async () => {
    const fix = vi.fn(async () => ({ ok: true }))
    window.dataHomeFixAPI = { available: vi.fn(async () => true), fix }
    vi.mocked(api.status)
      .mockResolvedValueOnce({ data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX } })
      .mockResolvedValue({ data_home_access: { ok: true, home: '/h', foreign: [], fix_command: '' } })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    fireEvent.click(await screen.findByTestId('data-home-fix-button'))
    expect(fix).toHaveBeenCalledWith()
    await waitFor(() => expect(screen.queryByTestId('settings-data-home-owner')).toBeNull())
    expect(api.status).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['cancelled', null],
    ['failed', "Couldn't fix the files. Run the command below in a terminal instead."],
  ])('a %s repair keeps the notice and the command', async (reason, message) => {
    window.dataHomeFixAPI = { available: vi.fn(async () => true), fix: vi.fn(async () => ({ ok: false, reason: reason as 'cancelled' | 'failed' })) }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    fireEvent.click(await screen.findByTestId('data-home-fix-button'))
    await waitFor(() => expect(screen.getByTestId('data-home-fix-button')).not.toBeDisabled())
    if (message) expect(await screen.findByTestId('data-home-fix-failed')).toHaveTextContent(message)
    else expect(screen.queryByTestId('data-home-fix-failed')).toBeNull()
    expect(screen.getByTestId('settings-data-home-owner').querySelector('code')?.textContent).toBe(FIX)
  })

  it.each([
    ['this account owns them', { data_home_access: { ok: true, home: '/h', foreign: [], fix_command: '' } }],
    ['an older gateway sends nothing', {}],
  ])('renders nothing when %s', async (_label, payload) => {
    vi.mocked(api.status).mockResolvedValue(payload)
    const { container } = renderWithProviders(<DataHomeOwnerStatusNotice />)
    await waitFor(() => expect(api.status).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
