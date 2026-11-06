import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import type { Artifact } from '../types'

vi.mock('../api/client')

/**
 * A static artifact published to the public web keeps its link in the AWS
 * account, not on the artifact. Its library entry reads the deploy's site
 * listing, so the link shows while the site exists and goes with it.
 */

const LIVE_URL = 'https://d111111abcdef8.cloudfront.net/'
const LINK_NAME = 'd111111abcdef8.cloudfront.net/'

const mk = (slug: string, name: string): Artifact => ({
  slug,
  name,
  kind: 'markdown',
  source: 'chat',
  pinned: false,
  description: '',
  content: '',
  tags: [],
  version: 1,
  created_at: '2026-11-01T10:00:00.000000+00:00',
  updated_at: '2026-11-01T10:00:00.000000+00:00',
})

function seed(artifacts: Artifact[], sites: { site_id: string; url: string }[]) {
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({ artifacts })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  m.artifact = vi.fn().mockImplementation((slug: string) =>
    Promise.resolve({ ...artifacts.find(a => a.slug === slug), content: '# Notes' }))
  m.themeBoot = vi.fn().mockResolvedValue({})
  m.publishProviders = vi.fn().mockResolvedValue({
    providers: [{ id: 'deploy-web-aws', endpoint: '/api/deploy/deploy' }],
  })
  m.deploySites = vi.fn().mockResolvedValue({ configured: true, sites })
  return m
}

/** Where the dashboard's router is, so a click that opened the artifact shows. */
function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

/** jsdom cannot follow a link; keep it from trying, without hiding the click. */
const noNavigation = (e: MouseEvent) => e.preventDefault()

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
  // Nothing on the page under test reaches the network itself; a stray bare
  // fetch answers not-ok rather than leaving the test environment.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  document.addEventListener('click', noNavigation, true)
})

afterEach(() => {
  document.removeEventListener('click', noNavigation, true)
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('ArtifactsPage — live public-web link', () => {
  it.each(['grid', 'table'])('links a deployed artifact from its %s entry until the site is gone', async (view) => {
    localStorage.setItem('mc-artifacts-view', view)
    const m = seed([mk('launch-notes', 'Launch Notes')], [{ site_id: 'launch-notes', url: LIVE_URL }])
    const { queryClient } = renderWithProviders(<><ArtifactsPage /><Where /></>, { route: '/artifacts' })
    const link = await screen.findByRole('link', { name: LINK_NAME })
    expect(link).toHaveAttribute('href', LIVE_URL)
    expect(link).toHaveAttribute('target', '_blank')

    // Following the link leaves the dashboard where it is.
    fireEvent.click(link)
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/artifacts$/)

    // Destroying the site on the Artifact Deploy page refreshes this same listing.
    m.deploySites.mockResolvedValue({ configured: true, sites: [] })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    })
    await waitFor(() => expect(screen.queryByRole('link', { name: LINK_NAME })).toBeNull())
    expect(screen.getByText('Launch Notes')).toBeInTheDocument()
  })

  it('links no listed URL that is not plain http(s)', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed([mk('launch-notes', 'Launch Notes'), mk('tampered-page', 'Tampered Page')], [
      { site_id: 'launch-notes', url: LIVE_URL },
      { site_id: 'tampered-page', url: 'javascript:alert(1)' },
    ])
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    // The safe site's link proves the listing has rendered.
    await screen.findByRole('link', { name: LINK_NAME })
    expect(screen.queryByRole('link', { name: /alert/ })).toBeNull()
  })

  it('reads no site listing while the Artifact Deploy preview is off', async () => {
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const m = seed([mk('launch-notes', 'Launch Notes')], [{ site_id: 'launch-notes', url: LIVE_URL }])
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(m.artifact).toHaveBeenCalledWith('launch-notes'))
    expect(m.deploySites).not.toHaveBeenCalled()
    expect(screen.queryByRole('link', { name: LINK_NAME })).toBeNull()
  })
})
