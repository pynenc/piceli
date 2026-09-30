import type { LogBatch } from '../../api/generated';

export type LogBuffer = {
  key: string;
  cursor: string | null;
  lines: string[];
  gap: boolean;
  dropped: number;
};

export const emptyLogBuffer = (key: string): LogBuffer => ({ key, cursor: null, lines: [], gap: false, dropped: 0 });

const size = (line: string) => (line.length + 1) * 2;

/** Merge two bounded tail reads. A missing overlap is a visible gap, never silent continuity. */
export function mergeLogBatch(current: LogBuffer, key: string, batch: LogBatch): LogBuffer {
  if (current.key !== key) current = emptyLogBuffer(key);
  if (current.cursor === batch.cursor) return current;
  let overlap = 0;
  if (current.lines.length && batch.lines.length) {
    const last = current.lines[current.lines.length - 1];
    for (let at = batch.lines.length - 1; at >= 0; at--) {
      if (batch.lines[at] !== last) continue;
      const count = at + 1;
      const start = current.lines.length - count;
      if (start >= 0 && current.lines.slice(start).every((line, index) => line === batch.lines[index])) {
        overlap = count;
        break;
      }
    }
  }
  const missing = current.lines.length > 0 && batch.lines.length > 0 && overlap === 0;
  const lines = missing ? [...current.lines, ...batch.lines] : [...current.lines, ...batch.lines.slice(overlap)];
  let bytes = lines.reduce((total, line) => total + size(line), 0);
  let dropped = current.dropped;
  while (lines.length > 20_000 || bytes > 8 * 1024 * 1024) {
    bytes -= size(lines.shift()!);
    dropped++;
  }
  return { key, cursor: batch.cursor, lines, gap: current.gap || batch.gap || missing || dropped > current.dropped, dropped };
}
