/** External provenance links are available only for an explicit GitHub repository. */
export function githubRepositoryUrl(repository: string | null | undefined): string | null {
  if (!repository || repository !== repository.trim() || /[\\\s%]/.test(repository) || /(?:^|\/)\.\.?(?:\/|$)/.test(repository)) return null;
  let candidate = repository;
  if (candidate.startsWith('git@github.com:')) candidate = `https://github.com/${candidate.slice('git@github.com:'.length)}`;
  if (candidate.startsWith('ssh://git@github.com/')) candidate = `https://github.com/${candidate.slice('ssh://git@github.com/'.length)}`;
  try {
    const url = new URL(candidate);
    if (url.protocol !== 'https:' || url.host !== 'github.com' || url.username || url.password || url.search || url.hash) return null;
    const path = url.pathname.replace(/\/$/, '').replace(/\.git$/, '');
    const parts = path.split('/');
    if (parts.length !== 3 || !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(parts[1]) || !/^[A-Za-z0-9_.-]+$/.test(parts[2]) || ['.', '..'].includes(parts[2])) return null;
    return `https://github.com/${parts[1]}/${parts[2]}`;
  } catch { return null; }
}

function sha(value: string | null | undefined): value is string {
  return Boolean(value && /^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$/.test(value));
}

export function githubCommitUrl(repository: string | null | undefined, revision: string | null | undefined): string | null {
  const base = githubRepositoryUrl(repository);
  return base && sha(revision) ? `${base}/commit/${revision}` : null;
}

export function githubCompareUrl(repository: string | null | undefined, before: string | null | undefined, after: string | null | undefined): string | null {
  const base = githubRepositoryUrl(repository);
  return base && sha(before) && sha(after) ? `${base}/compare/${before}...${after}` : null;
}
