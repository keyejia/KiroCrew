import { waitFor } from '@testing-library/react'

// Bare-factory mock, matching WelcomeView.brandMark.test.tsx: the ThemeProvider
// loads the active installed pack through these.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => new Promise(() => {}),
  },
}))

import { renderWithProviders } from './helpers'

const SLUG = 'harbor'
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

function installPack(branding: Record<string, string>) {
  localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
  themesFn.mockResolvedValue({
    themes: [{ slug: SLUG, name: 'Harbor', emoji: 'H', source: 'installed' }],
  })
  themeDetailFn.mockResolvedValue({
    slug: SLUG,
    name: 'Harbor',
    emoji: 'H',
    level: 1,
    source: 'installed',
    dark: { '--bg': '#000000' },
    light: { '--bg': '#ffffff' },
    assets: { branding },
  })
}

type Bridge = { electronAPI?: { setThemeSplash: (image: unknown) => void } }

describe("the desktop boot splash follows the active pack's splash art", () => {
  const setThemeSplash = vi.fn()
  const fetchMock = vi.fn()

  beforeEach(() => {
    localStorage.clear()
    themesFn.mockReset()
    themeDetailFn.mockReset()
    setThemeSplash.mockReset()
    fetchMock.mockReset()
    fetchMock.mockImplementation(async () => new Response(new Blob([PNG], { type: 'image/png' })))
    vi.stubGlobal('fetch', fetchMock)
    ;(window as unknown as Bridge).electronAPI = { setThemeSplash }
  })

  afterEach(() => {
    delete (window as unknown as Bridge).electronAPI
    vi.unstubAllGlobals()
    localStorage.clear()
    document.getElementById('mc-theme-favicon')?.remove()
    document.documentElement.style.removeProperty('--theme-logo')
  })

  it('hands the shell the bytes of the pack\'s branding/splash image', async () => {
    installPack({ splash: 'branding/splash.png' })
    renderWithProviders(<div />)
    await waitFor(() => expect(setThemeSplash).toHaveBeenLastCalledWith({ type: 'image/png', bytes: PNG }))
    expect(fetchMock).toHaveBeenCalledWith(`/api/theme/${SLUG}/assets/branding/splash.png`)
  })

  it('clears the shell copy for a pack that ships no splash image', async () => {
    installPack({ logo: 'branding/logo.svg' })
    renderWithProviders(<div />)
    // `--theme-logo` is set by the same branding pass that reports the splash.
    await waitFor(() =>
      expect(document.documentElement.style.getPropertyValue('--theme-logo')).not.toBe(''),
    )
    expect(setThemeSplash).toHaveBeenLastCalledWith(null)
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('splash'))
  })

  it('keeps the shell copy while the selected pack has not loaded', async () => {
    installPack({ splash: 'branding/splash.png' })
    themeDetailFn.mockReset()
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<div />)
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalledWith(SLUG))
    expect(setThemeSplash).not.toHaveBeenCalled()
  })
})
