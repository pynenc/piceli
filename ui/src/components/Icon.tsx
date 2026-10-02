import type { ReactNode } from 'react';

const paths: Record<string, ReactNode> = {
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
  attention: <><path d="m12 3 10 18H2L12 3Z" /><path d="M12 9v5m0 3h.01" /></>,
  changes: <><path d="M5 4v16m14-16v16M2 8h6m8 8h6M2 16h6m-3-3v6m11-11h6" /></>,
  history: <><path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2" /></>,
  overview: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
  environments: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
  applications: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M3 9h18M9 9v11" /></>,
  sources: <><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="5" r="2" /><path d="M6 7v10M18 7v2a6 6 0 0 1-6 6H6" /></>,
  pipeline: <><rect x="2" y="8" width="5" height="8" rx="1.5" /><rect x="17" y="8" width="5" height="8" rx="1.5" /><path d="M7 12h10m-6-3 3 3-3 3" /></>,
  build: <><path d="m12 3 9 5v9l-9 5-9-5V8l9-5ZM3 8l9 5 9-5M12 13v9M7.5 5.5l9 5" /></>,
  cluster: <><rect x="7" y="2" width="10" height="5" rx="1" /><rect x="2" y="17" width="8" height="5" rx="1" /><rect x="14" y="17" width="8" height="5" rx="1" /><path d="M12 7v5M6 17v-5h12v5" /></>,
  gitops: <><path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5" /></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  system: <><rect x="3" y="3" width="18" height="14" rx="2" /><path d="M8 21h8m-4-4v4" /></>,
};

/** Consistent decorative icons; the adjacent label remains the accessible name. */
export function Icon({ name }: { name: keyof typeof paths }) {
  return <svg className="icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}
