import { useContext, useEffect, useMemo, useRef, useState, type ContextType } from 'react'
import { UNSAFE_NavigationContext, parsePath, useSearchParams, type To } from 'react-router-dom'
import MarkdownPanel from '../components/MarkdownPanel'
import { writeFile } from '../hooks/usePanelDocumentActions'
import { appSpacePath } from './AppPopoutFrame'
import { HydratingFileTab } from './chat/SidePanelTabBodies'
import { closeSelf, forwardToMain } from '../utils/filePopout'

/**
 * The router's navigation context with every push and replace handed to the
 * main dashboard. The panel's own exits (an artifact, Open in Files) would
 * otherwise land on the pop-out route tree's catch-all, which mounts this
 * window afresh: the click does nothing and unsaved edits are lost. Same
 * scoping as the app pop-out's navigator, with no in-window destination.
 */
function useMainWindowNavigation(): ContextType<typeof UNSAFE_NavigationContext> {
  const navigation = useContext(UNSAFE_NavigationContext)
  return useMemo(() => {
    const exit = (to: To) => {
      // The router hands the navigator a path under its basename; the main
      // dashboard's navigate adds its own.
      const { pathname, search } = typeof to === 'string' ? parsePath(to) : to
      forwardToMain({ path: appSpacePath(pathname || '/', navigation.basename), search: search || undefined })
    }
    return { ...navigation, navigator: { ...navigation.navigator, push: exit, replace: exit } }
  }, [navigation])
}

/** The window's file: the buffer, what was last read from or written to disk,
 *  and that read's verdicts (`placeholder`: the buffer is the "file not found"
 *  sentence, until an edit or a disk read). */
interface PopoutDoc { text: string; saved: string; binary?: boolean; partial?: boolean; placeholder?: boolean }

/**
 * The window shell for a popped-out side-panel file
 * (`/popout/file?path=…&sid=…`, opened by `utils/filePopout`): the side
 * panel's own file viewer (`MarkdownPanel`), at the window's full width.
 *
 * Its own JS context. The first read is the side panel's own placeholder for
 * an unread file tab (`HydratingFileTab`); from then on the panel's file watch
 * keeps it current, and a save writes through the same `/api/file-write` the
 * side panel uses. `sid` is the chat the file was opened for, passed to both,
 * so the gateway answers this window as it answers the side panel. Closing the
 * window over unsaved edits asks first, as the artifact pop-out does, except
 * after the panel's own close has already asked.
 */
export default function FilePopoutFrame() {
  const [params] = useSearchParams()
  const path = params.get('path') ?? ''
  const slot = params.get('sid')
  const navigation = useMainWindowNavigation()
  const [doc, setDoc] = useState<PopoutDoc | null>(null)
  const dirty = !!doc && doc.text !== doc.saved
  const closing = useRef(false)

  useEffect(() => {
    if (!dirty) return
    const handler = (e: BeforeUnloadEvent) => { if (!closing.current) { e.preventDefault(); e.returnValue = '' } }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  const fromDisk = (text: string, binary?: boolean, partial?: boolean, placeholder?: boolean) =>
    setDoc(d => ({ text, saved: text, binary: binary ?? d?.binary, partial: partial ?? d?.partial, placeholder }))
  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col">
      {!doc ? (
        <HydratingFileTab path={path} slot={slot} onDiskContent={fromDisk} />
      ) : (
        <UNSAFE_NavigationContext.Provider value={navigation}>
          <MarkdownPanel
            embedded
            filePath={path}
            content={doc.text}
            savedBaseline={doc.saved}
            binary={doc.binary}
            partial={doc.partial}
            placeholder={doc.placeholder}
            onContentChange={text => setDoc(d => d && { ...d, text, placeholder: false })}
            onDiskContent={fromDisk}
            onSave={async (filePath, content) => {
              await writeFile(filePath, content, slot)
              setDoc(d => d && { ...d, saved: content })
            }}
            // The panel has asked about unsaved edits before it calls this.
            onClose={() => { closing.current = true; closeSelf() }}
            liveWatch
          />
        </UNSAFE_NavigationContext.Provider>
      )}
    </div>
  )
}
