import { describe, expect, it } from 'vitest';
import { reworkDiff } from '../fixtures/specimen';
import { parseDiff, unquotePath } from './parse';

describe('parseDiff', () => {
  const files = parseDiff(reworkDiff);

  it('reads modified, renamed, binary and quoted-path files', () => {
    expect(files.map((f) => [f.status, f.oldPath, f.newPath, f.binary])).toEqual([
      ['modified', 'frontend/src/jobs/poll.ts', 'frontend/src/jobs/poll.ts', false],
      ['renamed', 'frontend/src/jobs/labels.ts', 'frontend/src/jobs/state-labels.ts', false],
      ['added', null, 'frontend/public/plate-grain.png', true],
      ['modified', 'docs/样张.md', 'docs/样张.md', false],
    ]);
    expect(files[1]!.similarity).toBe(92);
  });

  it('numbers old and new lines from the hunk header', () => {
    const lines = files[0]!.hunks[0]!.lines;
    expect(lines[0]).toMatchObject({ kind: 'ctx', old: 36, new: 36 });
    expect(lines.find((l) => l.text.includes('job.error === null'))).toMatchObject({ kind: 'del', old: 39, new: null });
    const tail = files[0]!.hunks[1]!.lines;
    expect(tail[tail.length - 1]).toMatchObject({ kind: 'note', text: 'No newline at end of file' });
  });

  it('decodes octal escapes as UTF-8', () => {
    expect(unquotePath('"a/\\346\\240\\267\\345\\274\\240.md"')).toBe('a/样张.md');
    expect(unquotePath('plain/path.ts')).toBe('plain/path.ts');
  });
});
