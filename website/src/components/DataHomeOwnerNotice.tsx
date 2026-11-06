import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { canFixDataHome, fixDataHome } from '../lib/electron'
import type { StatusData } from '../types'
import ErrorNotice from './ErrorNotice'
import { CopyCommand } from './agentHarness/CopyCommand'
import { Btn } from './ui'

/** Re-read the data home's owner at most once a minute while a reader is mounted. */
const DATA_HOME_ACCESS_STALE_MS = 60_000
export const DATA_HOME_ACCESS_QUERY_KEY = ['data-home-access'] as const

/**
 * The desktop app's one-click repair: the OS asks for the admin password and the
 * data home is given back to this account. Renders nothing where the shell
 * cannot do it (a plain browser, the PWA, a window on a remote gateway), so the
 * command below it stays the way there.
 */
export function DataHomeFixButton({ onFixed }: { onFixed?: () => void }) {
  const queryClient = useQueryClient()
  const [available, setAvailable] = useState(false)
  const [state, setState] = useState<'idle' | 'fixing' | 'fixed' | 'failed'>('idle')
  useEffect(() => {
    let live = true
    void canFixDataHome().then(ok => { if (live) setAvailable(ok) })
    return () => { live = false }
  }, [])
  if (!available) return null
  const run = async () => {
    setState('fixing')
    const outcome = await fixDataHome()
    if (outcome === 'fixed') {
      setState('fixed')
      await queryClient.invalidateQueries({ queryKey: DATA_HOME_ACCESS_QUERY_KEY })
      onFixed?.()
      return
    }
    setState(outcome === 'cancelled' ? 'idle' : 'failed')
  }
  return (
    <div className="mt-2 flex flex-col gap-1" data-testid="data-home-fix">
      <div>
        <Btn onClick={() => { void run() }} disabled={state === 'fixing'} data-testid="data-home-fix-button">
          {state === 'fixing' ? i18nT('components.dataHomeOwnerNotice.fixing') : i18nT('components.dataHomeOwnerNotice.fix_button')}
        </Btn>
      </div>
      {state === 'fixed' && <p className="text-[13px] text-ok" role="status">{i18nT('components.dataHomeOwnerNotice.fixed')}</p>}
      {state === 'failed' && <p className="text-[13px] text-danger" role="alert" data-testid="data-home-fix-failed">{i18nT('components.dataHomeOwnerNotice.fix_failed')}</p>}
    </div>
  )
}

/**
 * Some files in the data home belong to another account (an earlier
 * `sudo kirocrew ...`), so this account cannot read them and a new Bot cannot be
 * created. Says so in one sentence and gives the gateway's own repair line to copy.
 */
export function DataHomeOwnerNotice({ fixCommand, testId = 'data-home-owner-notice' }: {
  fixCommand: string
  testId?: string
}) {
  return (
    <div data-testid={testId}>
      <ErrorNotice message={i18nT('components.dataHomeOwnerNotice.message')} variant="inline" />
      <DataHomeFixButton />
      <p className="mt-2 text-[13px] text-muted">{i18nT('components.dataHomeOwnerNotice.run_this')}</p>
      <CopyCommand><code>{fixCommand}</code></CopyCommand>
    </div>
  )
}

/** The same notice, shown only while `/api/status` reports the problem. */
export function DataHomeOwnerStatusNotice() {
  const { data } = useQuery({
    queryKey: DATA_HOME_ACCESS_QUERY_KEY,
    queryFn: async () => ((await api.status()) as StatusData).data_home_access ?? null,
    staleTime: DATA_HOME_ACCESS_STALE_MS,
  })
  if (!data || data.ok || !data.fix_command) return null
  return (
    <div className="mb-4">
      <DataHomeOwnerNotice fixCommand={data.fix_command} testId="settings-data-home-owner" />
    </div>
  )
}
