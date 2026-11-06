/**
 * Hand the active installed theme's boot-splash art (`branding/splash.*`) to
 * the desktop shell, which shows it on the next loading.html paint, before any
 * gateway is up to serve the pack (website/electron/theme-splash.js). A null
 * `url`, or art that cannot be read, clears the shell's copy so the stock
 * splash returns. `superseded` drops the answer of a fetch a later theme switch
 * overtook. A no-op in a plain browser.
 */
export function reportThemeSplash(url: string | null, superseded: () => boolean): void {
  const setThemeSplash = window.electronAPI?.setThemeSplash
  if (!setThemeSplash) return
  if (!url) {
    setThemeSplash(null)
    return
  }
  fetch(url)
    .then((res) => (res.ok ? res.blob() : null))
    .then(async (blob) => (blob ? { type: blob.type, bytes: new Uint8Array(await blob.arrayBuffer()) } : null))
    .catch(() => null)
    .then((image) => {
      if (!superseded()) setThemeSplash(image)
    })
}
