import { useEffect, useRef, useState } from 'react';
import { useNavigationType, useSearchParams } from 'react-router-dom';

/** Keystrokes commit locally before asynchronous router updates can settle. */
export function useUrlText(key: string): [string, (value: string) => void] {
  const [params, setParams] = useSearchParams();
  const urlValue = params.get(key) ?? '';
  const navigationType = useNavigationType();
  const [value, setValue] = useState(urlValue);
  const pending = useRef<string | null>(null);
  useEffect(() => {
    if (navigationType === 'POP') {
      pending.current = null;
      setValue(urlValue);
    } else if (pending.current === null) {
      setValue(urlValue);
    } else if (urlValue === pending.current) {
      pending.current = null;
    }
    // Intermediate URL commits must not overwrite a more recent keystroke.
  }, [urlValue, navigationType]);
  return [value, nextValue => {
    setValue(nextValue);
    pending.current = nextValue;
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (nextValue) next.set(key, nextValue); else next.delete(key);
      return next;
    }, { replace: true });
  }];
}
