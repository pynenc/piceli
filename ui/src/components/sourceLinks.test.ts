import { describe, expect, it } from 'vitest';
import { githubCommitUrl, githubCompareUrl, githubRepositoryUrl } from './sourceLinks';

describe('explicit GitHub provenance', () => {
  it.each(['https://github.com/example/shop', 'https://github.com/example/shop.git', 'git@github.com:example/shop.git', 'ssh://git@github.com/example/shop.git'])('normalizes %s', repository => {
    expect(githubRepositoryUrl(repository)).toBe('https://github.com/example/shop');
    expect(githubCommitUrl(repository, 'a'.repeat(40))).toBe(`https://github.com/example/shop/commit/${'a'.repeat(40)}`);
  });
  it.each([undefined, '', 'release.py:release', 'javascript:alert(1)', 'http://github.com/example/shop', 'https://github.com.evil.example/example/shop', 'https://github.com@evil.example/example/shop', 'https://user:secret@github.com/example/shop', 'https://github.com/example/shop?token=secret', 'https://github.com/example/shop#fragment', 'https://github.com/example/shop/tree/main', 'https://github.com/example/%2e%2e', 'git@other.example:example/shop.git', 'https://github.com/example\\shop', ' https://github.com/example/shop'])('does not link an unsafe or ambiguous repository %s', repository => {
    expect(githubRepositoryUrl(repository)).toBeNull();
    expect(githubCommitUrl(repository, 'a'.repeat(40))).toBeNull();
  });
  it('requires exact object hashes for commits and both comparison endpoints', () => {
    const repository = 'https://github.com/example/shop';
    expect(githubCompareUrl(repository, 'a'.repeat(40), 'b'.repeat(64))).toBe(`${repository}/compare/${'a'.repeat(40)}...${'b'.repeat(64)}`);
    for (const value of [null, 'main', 'a'.repeat(7), 'a'.repeat(39), 'a'.repeat(65), '../commit', `${'a'.repeat(40)}?token=secret`]) {
      expect(githubCommitUrl(repository, value)).toBeNull();
      expect(githubCompareUrl(repository, value, 'b'.repeat(40))).toBeNull();
      expect(githubCompareUrl(repository, 'b'.repeat(40), value)).toBeNull();
    }
  });
  it('does not normalize traversal into a different repository', () => {
    expect(githubRepositoryUrl('https://github.com/example/../other/shop')).toBeNull();
  });
});
