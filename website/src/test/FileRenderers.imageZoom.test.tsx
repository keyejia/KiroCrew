/**
 * The file viewer's image zoom and pan: Alt/Opt+wheel zooms about the cursor, a
 * drag pans a zoomed image, and the view comes back when the same file is
 * reopened in this tab, unless the image's pixel size changed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ImageViewer } from '../components/FileRenderers'

const rect = (left: number, top: number, w: number, h: number) =>
  ({ left, top, right: left + w, bottom: top + h, width: w, height: h, x: left, y: top, toJSON: () => ({}) }) as DOMRect

/** The viewer remembers views in module memory for the tab's lifetime, so every
 *  run (a `--repeat` included) opens paths no earlier run has zoomed. */
let run = 0
const file = (name: string) => `/zzq/${name}-${run}.png`

/** Open `path` and load it at `w`x`h` pixels. Laid out, the box is 1600x1200,
 *  centred on the 1024x768 screen, so a zoomed image has room to pan; not laid
 *  out, it has no box at all, as in a tab mounted hidden. */
function open(path: string, w: number, h: number, laidOut = true) {
  const view = render(<ImageViewer filePath={path} />)
  const img = screen.getByRole('img') as HTMLImageElement
  if (laidOut) {
    Object.defineProperty(img, 'offsetWidth', { configurable: true, value: 1600 })
    Object.defineProperty(img, 'offsetHeight', { configurable: true, value: 1200 })
    vi.spyOn(img, 'getBoundingClientRect').mockReturnValue(rect(-288, -216, 1600, 1200))
  }
  Object.defineProperty(img, 'naturalWidth', { configurable: true, value: w })
  Object.defineProperty(img, 'naturalHeight', { configurable: true, value: h })
  img.setPointerCapture = () => {}
  fireEvent.load(img)
  return { img, unmount: view.unmount }
}

/** One wheel notch up at the screen centre (factor 1.25). The modifier and the
 *  coordinates are assigned after construction: the test DOM's `WheelEvent`
 *  drops every field it inherits from MouseEvent, and `fireEvent.wheel` too. */
function wheelIn(img: HTMLElement, altKey: boolean): boolean {
  const e = new WheelEvent('wheel', { deltaY: -100, bubbles: true, cancelable: true })
  Object.defineProperty(e, 'altKey', { value: altKey })
  Object.defineProperty(e, 'clientX', { value: 512 })
  Object.defineProperty(e, 'clientY', { value: 384 })
  return fireEvent(img, e)
}

function drag(img: HTMLElement, dx: number, dy: number) {
  fireEvent.pointerDown(img, { pointerId: 1, button: 0, clientX: 500, clientY: 400 })
  fireEvent.pointerMove(img, { pointerId: 1, clientX: 500 + dx, clientY: 400 + dy })
  fireEvent.pointerUp(img, { pointerId: 1, clientX: 500 + dx, clientY: 400 + dy })
}

beforeEach(() => {
  run++
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 768 })
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('ImageViewer zoom and pan', () => {
  it('zooms on Alt+wheel only, and pans a zoomed image by drag', () => {
    const { img } = open(file('zoom'), 800, 600)
    // A plain wheel is the pane's scroll, not a zoom.
    expect(wheelIn(img, false)).toBe(true)
    expect(img.style.transform).toBe('translate(0px, 0px) scale(1)')
    expect(img.className).not.toContain('cursor-grab')

    expect(wheelIn(img, true)).toBe(false)
    expect(img.style.transform).toBe('translate(0px, 0px) scale(1.25)')
    expect(img.className).toContain('cursor-grab')

    fireEvent.pointerDown(img, { pointerId: 1, button: 0, clientX: 500, clientY: 400 })
    expect(img.className).toContain('cursor-grabbing')
    fireEvent.pointerMove(img, { pointerId: 1, clientX: 550, clientY: 440 })
    fireEvent.pointerUp(img, { pointerId: 1, clientX: 550, clientY: 440 })
    expect(img.style.transform).toBe('translate(50px, 40px) scale(1.25)')
    expect(img.className).not.toContain('cursor-grabbing')
  })

  it('restores the view on reopen, hidden or not, and drops it when the pixel size changed', () => {
    const path = file('regen')
    const first = open(path, 800, 600)
    wheelIn(first.img, true)
    drag(first.img, 50, 40)
    first.unmount()

    const hidden = open(path, 800, 600, false)
    expect(hidden.img.style.transform).toBe('translate(50px, 40px) scale(1.25)')
    hidden.unmount()

    const again = open(path, 800, 600)
    expect(again.img.style.transform).toBe('translate(50px, 40px) scale(1.25)')
    again.unmount()

    const regenerated = open(path, 1024, 768)
    expect(regenerated.img.style.transform).toBe('translate(0px, 0px) scale(1)')
    regenerated.unmount()

    // The stale view is gone, not just skipped: the old size starts at fit too.
    expect(open(path, 800, 600).img.style.transform).toBe('translate(0px, 0px) scale(1)')
  })
})
