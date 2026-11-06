import { useQuery } from '@tanstack/react-query'
import { Globe } from 'lucide-react'
import { api } from '../../api/client'
import { findLiveSite } from '../PublishHub'
import { safeHttpUrl } from '../../lib/safeUrl'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_ARTIFACT_DEPLOY } from '../../utils/previewFlags'

/**
 * The link to an artifact's public-web site, on its library entry.
 *
 * Read from the deploy's own site listing, the same query and cache entry the
 * Publish panel and the Artifact Deploy page use, so nothing is stored on the
 * artifact and a destroyed or reaped site takes its link with it. Every entry
 * shares that one request. Renders nothing until the Artifact Deploy preview is
 * on and the listing names a site for this slug.
 */
export function LiveSiteLink({ slug }: { slug: string }) {
  const deployPreview = usePreviewFlag(PREVIEW_ARTIFACT_DEPLOY)
  const { data } = useQuery({
    queryKey: ['deploy-web', 'sites'],
    queryFn: () => api.deploySites(),
    enabled: deployPreview,
    staleTime: 30_000,
    // The library stays open for long stretches, and each read reaches every
    // registered AWS account: re-read on a mount or a deploy, not on every focus.
    refetchOnWindowFocus: false,
    retry: false,
  })
  const raw = deployPreview ? findLiveSite(data, slug)?.url : undefined
  const url = raw ? safeHttpUrl(raw) : null
  if (!url) return null
  const { host, pathname } = new URL(url)
  return (
    // `relative` lifts it above the gallery card's open button; the click stays
    // off the table row, whose own click opens the artifact. Not draggable, so
    // a press-and-drag here still drags the entry into a folder.
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={url}
      draggable={false}
      onClick={(e) => e.stopPropagation()}
      className="relative flex items-center gap-1 mt-0.5 max-w-[min(100%,400px)] w-fit text-[11px] text-info hover:underline"
    >
      <Globe className="lucide-inline" aria-hidden="true" />
      <span className="truncate font-mono">{`${host}${pathname}`}</span>
    </a>
  )
}
