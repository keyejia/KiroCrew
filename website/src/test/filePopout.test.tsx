/**
 * A side-panel file pops out into its own window (#25804): the file preview's
 * ⋯ menu opens `/popout/file` for that file and the chat it was opened for,
 * and that window is the same file viewer, reading and saving the file for
 * that chat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'
import { i18nT } from '../i18n/t'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: () => <div data-testid="pierre-code" />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))
vi.mock('../components/CodeEditor', () => ({
  CodeEditor: ({ content, onChange }: { content: string; onChange: (v: string) => void }) => (
    <textarea aria-label="source" data-testid="code-editor" value={content} onChange={e => onChange(e.target.value)} />
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    // `/w/linked.py` is already in the artifact library, so its header offers Open as artifact.
    artifacts: vi.fn(async ({ source_path }: { source_path: string }) => ({
      artifacts: source_path === '/w/linked.py' ? [{ slug: 'linked', name: 'Linked' }] : [],
    })),
    artifact: vi.fn().mockResolvedValue({ live_dirty: false, pinned: false }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))
vi.mock('../utils/filePopout', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/filePopout')>()),
  forwardToMain: vi.fn(),
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')
const { default: FilePopoutFrame } = await import('../pages/FilePopoutFrame')
const { openFilePopout, forwardToMain } = await import('../utils/filePopout')

const fetchMock = vi.fn(async (input: unknown, _init?: RequestInit) => {
  const url = String(input)
  if (url.startsWith('/api/knowledge/config')) return { ok: true, json: async () => ({ enabled: false, supported_formats: [] }) }
  if (url.startsWith('/api/knowledge/sources')) return { ok: true, json: async () => [] }
  return { ok: true, status: 200, headers: { get: () => null }, text: async () => 'print(1)\n', json: async () => ({}) }
})

function renderIn(node: ReactNode, entry = '/', basename?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createTestStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[entry]} basename={basename}>{node}</MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

const popOutRow = () => screen.getByRole('menuitem', { name: i18nT('components.markdownPanel.pop_out_to_window') })

/** Whether closing the window now would ask first. */
function closeAsks(): boolean {
  const e = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(e)
  return e.defaultPrevented
}

beforeEach(() => {
  localStorage.clear()
  fetchMock.mockClear()
  vi.mocked(forwardToMain).mockClear()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('popping a side-panel file out to its own window', () => {
  it('opens the file in its own window, for the chat it was opened for', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({ focus: vi.fn(), closed: false } as unknown as Window)
    renderIn(
      <MarkdownPanel embedded filePath="/w/main.py" content={'print(1)\n'} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
        onClose={vi.fn()} onPopout={() => openFilePopout('/w/main.py', 'chat-1')} />,
    )
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(popOutRow())
    expect(open).toHaveBeenCalledTimes(1)
    expect(open.mock.calls[0][0]).toBe(`${window.location.origin}/popout/file?path=%2Fw%2Fmain.py&sid=chat-1`)
  })

  it('gives two files whose paths differ only in punctuation a window each', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => ({ focus: vi.fn(), closed: false }) as unknown as Window)
    openFilePopout('/w/a b.md', null)
    openFilePopout('/w/a_b.md', null)
    expect(open).toHaveBeenCalledTimes(2)
    expect(open.mock.calls[0][1]).not.toBe(open.mock.calls[1][1])
  })

  it('waits for unsaved edits, since the new window reads the file from disk', () => {
    const onPopout = vi.fn()
    renderIn(
      <MarkdownPanel embedded filePath="/w/main.py" content={'print(2)\n'} savedBaseline={'print(1)\n'} onContentChange={vi.fn()}
        onSave={vi.fn(async () => {})} onClose={vi.fn()} onPopout={onPopout} />,
    )
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    expect(popOutRow()).toBeDisabled()
    fireEvent.click(popOutRow())
    expect(onPopout).not.toHaveBeenCalled()
  })

  it('the window reads the file for its chat, asks before closing over edits, and saves through that chat', async () => {
    renderIn(<FilePopoutFrame />, '/popout/file?path=%2Fw%2Fmain.py&sid=chat-1')
    const editor = await screen.findByTestId('code-editor')
    expect(editor).toHaveValue('print(1)\n')
    expect(fetchMock).toHaveBeenCalledWith('/api/file-read?path=%2Fw%2Fmain.py&slot=chat-1', expect.anything())
    expect(closeAsks()).toBe(false)

    fireEvent.change(editor, { target: { value: 'print(2)\n' } })
    expect(closeAsks()).toBe(true)

    await act(async () => { fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0]) })
    expect(fetchMock).toHaveBeenCalledWith('/api/file-write?slot=chat-1', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ path: '/w/main.py', content: 'print(2)\n' }),
    }))
    await vi.waitFor(() => expect(closeAsks()).toBe(false))
  })

  it("hands the panel's own exits to the main dashboard and keeps the window's unsaved edits", async () => {
    // Served under a sub-path: the main dashboard is handed the path without it.
    renderIn(<FilePopoutFrame />, '/crew/popout/file?path=%2Fw%2Flinked.py&sid=chat-1', '/crew')
    const editor = await screen.findByTestId('code-editor')
    fireEvent.change(editor, { target: { value: 'print(2)\n' } })
    const openArtifact = await screen.findAllByRole('button', { name: i18nT('components.markdownPanel.open_as_artifact') })
    fireEvent.click(openArtifact[0])
    expect(forwardToMain).toHaveBeenCalledWith({ path: '/artifacts/linked', search: undefined })
    expect(screen.getByTestId('code-editor')).toHaveValue('print(2)\n')
    expect(closeAsks()).toBe(true)
  })
})
