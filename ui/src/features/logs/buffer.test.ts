import { describe, expect, it } from 'vitest';
import { emptyLogBuffer, mergeLogBatch } from './buffer';

describe('bounded log continuity', () => {
  it('keeps an overlapping tail once and reports a missing interval', () => {
    const first = mergeLogBatch(emptyLogBuffer('pod/api'), 'pod/api', { cursor: 'a', lines: ['1', '2', '3'] });
    const continued = mergeLogBatch(first, 'pod/api', { cursor: 'b', lines: ['2', '3', '4'] });
    expect(continued.lines).toEqual(['1', '2', '3', '4']);
    expect(continued.gap).toBe(false);
    const missed = mergeLogBatch(continued, 'pod/api', { cursor: 'c', lines: ['7', '8'] });
    expect(missed.lines).toEqual(['1', '2', '3', '4', '7', '8']);
    expect(missed.gap).toBe(true);
    expect(mergeLogBatch(missed, 'other/current', { cursor: 'd', lines: ['new'] }).lines).toEqual(['new']);
  });

  it('caps retained lines and exposes known dropped history', () => {
    const burst = mergeLogBatch(emptyLogBuffer('pod/api'), 'pod/api', {
      cursor: 'burst', lines: Array.from({ length: 20_010 }, (_, index) => String(index)),
    });
    expect(burst.lines).toHaveLength(20_000);
    expect(burst.lines[0]).toBe('10');
    expect(burst.dropped).toBe(10);
    expect(burst.gap).toBe(true);
  });

  it('retains a ten-second 1,000-line-per-second burst when reads overlap', () => {
    let buffer = emptyLogBuffer('pod/api');
    for (let second = 1; second <= 10; second++) {
      const last = second * 1000;
      buffer = mergeLogBatch(buffer, 'pod/api', {
        cursor: String(second),
        lines: Array.from({ length: Math.min(2000, last) }, (_, index) => String(last - Math.min(2000, last) + index)),
      });
    }
    expect(buffer.lines).toHaveLength(10_000);
    expect(buffer.gap).toBe(false);
    expect(buffer.lines.at(-1)).toBe('9999');
    const afterPause = mergeLogBatch(buffer, 'pod/api', {
      cursor: 'after-pause',
      lines: Array.from({ length: 2000 }, (_, index) => String(13_000 + index)),
    });
    expect(afterPause.gap).toBe(true);
  });
});
