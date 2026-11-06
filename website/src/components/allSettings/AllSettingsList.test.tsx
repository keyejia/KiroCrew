import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { AllSettingsList, valueAt } from './AllSettingsList'

const ENTRIES = [
  {
    path: 'mcp_gateway.keep_alive_servers', type: 'array', label: 'Keep-alive servers',
    help: 'Servers to keep running.', defaultValue: [],
    allSettings: { mode: 'edit', control: 'str_list', resettable: true },
  },
  {
    path: 'mcp_gateway.idle_timeout_secs', type: 'integer', label: 'Idle timeout',
    help: '', defaultValue: 300, requiresRestart: true,
    allSettings: { mode: 'edit', control: 'int', resettable: true, min: 0, max: 1e12 },
  },
  {
    path: 'agent.streaming', type: 'boolean', label: 'Streaming',
    help: '', defaultValue: true,
    allSettings: { mode: 'edit', control: 'bool', resettable: true },
  },
  {
    path: 'agent.dangerously_skip_permissions', type: 'boolean', label: 'Skip approvals',
    help: '', defaultValue: false, requiresRestart: true, allSettings: { mode: 'readonly' },
  },
  // Not listed: the gateway sent no allSettings record.
  { path: 'agent.env', type: 'object', label: 'Agent env', help: '', defaultValue: {} },
]

const CONFIG = {
  mcp_gateway: { keep_alive_servers: ['github'], idle_timeout_secs: 300 },
  agent: { streaming: true, dangerously_skip_permissions: false },
}

function rowFor(key: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-setting-key="${key}"]`)
  if (!row) throw new Error(`no row for ${key}`)
  return row
}

describe('AllSettingsList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: ENTRIES }), { status: 200 })))
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue(CONFIG)
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(CONFIG)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('lists only keys the gateway marks, with the value and default', async () => {
    renderWithProviders(<AllSettingsList />)
    await screen.findByText('Keep-alive servers')
    expect(screen.queryByText('Agent env')).toBeNull()
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Applies after restart')).toBeTruthy()
    // A restart key says when its saved value takes effect, instead of "Saves at once" alone.
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Saves at once; applies after a gateway restart')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).getByText('Read only')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByRole('switch')).toBeNull()
  })

  it('saves a list through the config route, one item per line', async () => {
    renderWithProviders(<AllSettingsList />)
    const box = await screen.findByLabelText('Keep-alive servers')
    fireEvent.change(box, { target: { value: 'github\n  slack \n\n' } })
    fireEvent.click(within(rowFor('mcp_gateway.keep_alive_servers')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', ['github', 'slack'], undefined))
  })

  it('saves a number as a number and resets a changed key with unset', async () => {
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '120' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.idle_timeout_secs', 120, undefined))

    fireEvent.click(within(rowFor('mcp_gateway.keep_alive_servers')).getByRole('button', { name: /Reset mcp_gateway.keep_alive_servers/ }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', undefined, { unset: true }))
    // A key at its default still shows its reset, disabled, so the undo is visible before any change.
    const atDefault = within(rowFor('agent.streaming')).getByRole('button', { name: /Reset agent.streaming/ })
    expect((atDefault as HTMLButtonElement).disabled).toBe(true)
    expect(within(rowFor('agent.streaming')).getByText('Saves at once')).toBeTruthy()
    // A read-only row labels its value and drops the restart badge.
    expect(within(rowFor('agent.dangerously_skip_permissions')).getByText('Current: Disabled')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByText('Applies after restart')).toBeNull()
  })

  it('marks a refused save as not saved and puts the saved value back in one click', async () => {
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('must be between 0 and 10'))
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '-5' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const row = rowFor('mcp_gateway.idle_timeout_secs')
    await within(row).findByText('Not saved')
    expect(field.getAttribute('aria-invalid')).toBe('true')
    // "Saves at once" would contradict the refusal.
    expect(within(row).queryByText('Saves at once')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Restore saved value (300)' }))
    await waitFor(() => expect((screen.getByLabelText('Idle timeout') as HTMLInputElement).value).toBe('300'))
    expect(within(row).queryByText('Not saved')).toBeNull()
  })

  it('shows what the gateway holds after a save the loader clamped back to the old value', async () => {
    // The gateway accepts 2000 but its loader clamps the field, so the reread value is still 300.
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '2000' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.idle_timeout_secs', 2000, undefined))
    await waitFor(() => expect((screen.getByLabelText('Idle timeout') as HTMLInputElement).value).toBe('300'))
  })

  it('filters by key or label', async () => {
    renderWithProviders(<AllSettingsList />)
    await screen.findByText('Streaming')
    fireEvent.change(screen.getByLabelText('Filter by name, key or description'), { target: { value: 'idle_timeout' } })
    expect(screen.queryByText('Streaming')).toBeNull()
    expect(screen.getByText('Idle timeout')).toBeTruthy()
  })
})

describe('valueAt', () => {
  it('walks a dotted path and stops at a non-object', () => {
    expect(valueAt({ a: { b: 1 } }, 'a.b')).toBe(1)
    expect(valueAt({ a: 3 }, 'a.b')).toBeUndefined()
  })
})
