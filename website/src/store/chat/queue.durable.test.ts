/**
 * The server's `meta.durable: false` verdict rides the queued row on both doors
 * (the live `queue_push` frame and the slot-detail queue a refresh rebuilds
 * from), so the card's "won't survive a restart" label outlives a refresh.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage } from '../chatSlice'
import { hydrateQueuedBubbles } from './queue'
import { normalizeQueueEntries } from './wire'

const slot = 'durable-slot'
const ts = '2026-11-06T09:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })

describe('queued row durable verdict', () => {
  it('rides a pushed card only when the server says false', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'kept', ts, queue_id: 'q1' }))
    state = reducer(state, appendQueuedMessage({ slot, content: 'refused', ts, queue_id: 'q2', meta: { durable: false } }))
    const rows = state.slotMessages[slot] ?? []
    expect(rows.map(r => r.meta?.durable)).toEqual([undefined, false])
  })

  it('rides a card rebuilt from the slot-detail queue', () => {
    const queue = normalizeQueueEntries([{ id: 'q1', content: 'kept' }, { id: 'q2', content: 'refused', meta: { durable: false } }])
    expect(hydrateQueuedBubbles([], queue).map(r => r.meta?.durable)).toEqual([undefined, false])
  })
})
