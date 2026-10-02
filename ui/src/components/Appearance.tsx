import { useEffect, useState } from 'react';
import { Icon } from './Icon';

type Theme = 'system' | 'light' | 'dark';
const preferenceKey = 'piceli.appearance';
function readPreference(): Theme {
  try {
    const stored = localStorage.getItem(preferenceKey);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch { return 'system'; }
}

export function Appearance() {
  const [theme, setTheme] = useState<Theme>(readPreference);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(preferenceKey, theme); } catch { /* Preference is optional in restricted browsers. */ }
  }, [theme]);
  return <label className="appearance"><Icon name="system" /><span className="sr-only">Appearance</span><select value={theme} onChange={event => setTheme(event.target.value as Theme)}><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select></label>;
}
