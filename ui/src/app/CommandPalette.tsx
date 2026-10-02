import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { Capabilities } from '../api/generated';
import { Icon } from '../components/Icon';
import './command-palette.css';

const CommandDialog = lazy(async () => ({ default: (await import('./CommandDialog')).CommandDialog }));

export function CommandPalette({ capabilities }: { capabilities?: Capabilities }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setOpen(current => !current); }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);
  return <><button ref={trigger} className="command-trigger" aria-label="Search workspace" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}><Icon name="search" /><span>Find in workspace</span><kbd>⌘ K</kbd></button>
    {open && <Suspense fallback={<span className="sr-only" role="status">Opening workspace search…</span>}><CommandDialog capabilities={capabilities} onClose={() => setOpen(false)} restoreFocus={() => trigger.current?.focus()} /></Suspense>}
  </>;
}
