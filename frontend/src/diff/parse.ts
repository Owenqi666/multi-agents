// Unified diff text from git, parsed for display. The bridge returns the text as is, so
// this handles rename and copy headers, binary files, mode-only changes, "\ No newline"
// markers and C-quoted paths ("a/\344\270\255.txt").

export type LineKind = 'add' | 'del' | 'ctx' | 'note';

export interface DiffLine {
  kind: LineKind;
  text: string;
  old: number | null;
  new: number | null;
}

export interface DiffHunk {
  header: string;
  section: string;
  lines: DiffLine[];
}

export type FileStatus = 'modified' | 'added' | 'deleted' | 'renamed' | 'copied';

export interface DiffFile {
  oldPath: string | null;
  newPath: string | null;
  status: FileStatus;
  binary: boolean;
  similarity: number | null;
  modeChange: string | null;
  hunks: DiffHunk[];
}

/** Undo git's C-style quoting; octal escapes are UTF-8 bytes. */
export function unquotePath(raw: string): string {
  if (!raw.startsWith('"') || !raw.endsWith('"')) return raw;
  const body = raw.slice(1, -1);
  const bytes: number[] = [];
  const simple: Record<string, number> = { n: 10, t: 9, r: 13, '"': 34, '\\': 92, a: 7, b: 8, f: 12, v: 11 };
  for (let i = 0; i < body.length; i++) {
    const ch = body[i]!;
    if (ch !== '\\') {
      bytes.push(...new TextEncoder().encode(ch));
      continue;
    }
    const next = body[i + 1] ?? '';
    if (/[0-7]/.test(next)) {
      const oct = body.slice(i + 1, i + 4);
      bytes.push(parseInt(oct, 8));
      i += 3;
    } else {
      bytes.push(simple[next] ?? next.charCodeAt(0));
      i += 1;
    }
  }
  return new TextDecoder('utf-8').decode(new Uint8Array(bytes));
}

/** Strip the a/ or b/ prefix from a path token, after unquoting. */
function side(token: string): string | null {
  const t = token.trim();
  if (t === '/dev/null') return null;
  const p = unquotePath(t);
  return p.startsWith('a/') || p.startsWith('b/') ? p.slice(2) : p;
}

/** Paths from "diff --git a/x b/y" when no ---/+++ lines follow (binary, mode, pure rename). */
function headerPaths(rest: string): [string | null, string | null] {
  const quoted = /^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$/.exec(rest);
  if (quoted) return [side(quoted[1]!), side(quoted[2]!)];
  const half = rest.indexOf(' b/');
  if (half > 0) return [side(rest.slice(0, half)), side(rest.slice(half + 1))];
  return [null, null];
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  const lines = text.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();

  for (const line of lines) {
    if (line.startsWith('diff --git ')) {
      const [a, b] = headerPaths(line.slice('diff --git '.length));
      file = { oldPath: a, newPath: b, status: 'modified', binary: false, similarity: null, modeChange: null, hunks: [] };
      files.push(file);
      hunk = null;
      continue;
    }
    if (!file) continue;
    if (hunk) {
      const c = line.charAt(0);
      if (c === '+') { hunk.lines.push({ kind: 'add', text: line.slice(1), old: null, new: newNo++ }); continue; }
      if (c === '-') { hunk.lines.push({ kind: 'del', text: line.slice(1), old: oldNo++, new: null }); continue; }
      if (c === ' ' || line === '') { hunk.lines.push({ kind: 'ctx', text: line.slice(1), old: oldNo++, new: newNo++ }); continue; }
      if (c === '\\') { hunk.lines.push({ kind: 'note', text: line.slice(2), old: null, new: null }); continue; }
    }
    const h = HUNK.exec(line);
    if (h) {
      oldNo = Number(h[1]);
      newNo = Number(h[3]);
      hunk = { header: line, section: h[5] ?? '', lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    hunk = null;
    if (line.startsWith('--- ')) file.oldPath = side(line.slice(4));
    else if (line.startsWith('+++ ')) file.newPath = side(line.slice(4));
    else if (line.startsWith('new file mode')) file.status = 'added';
    else if (line.startsWith('deleted file mode')) file.status = 'deleted';
    else if (line.startsWith('rename from ')) { file.status = 'renamed'; file.oldPath = unquotePath(line.slice(12)); }
    else if (line.startsWith('rename to ')) file.newPath = unquotePath(line.slice(10));
    else if (line.startsWith('copy from ')) { file.status = 'copied'; file.oldPath = unquotePath(line.slice(10)); }
    else if (line.startsWith('copy to ')) file.newPath = unquotePath(line.slice(8));
    else if (line.startsWith('similarity index ')) file.similarity = Number.parseInt(line.slice(17), 10);
    else if (line.startsWith('old mode ')) file.modeChange = line.slice(9);
    else if (line.startsWith('new mode ')) file.modeChange = `${file.modeChange ?? ''} → ${line.slice(9)}`;
    else if (line.startsWith('Binary files ') || line === 'GIT binary patch') file.binary = true;
  }
  for (const f of files) {
    if (f.status === 'added') f.oldPath = null;
    if (f.status === 'deleted') f.newPath = null;
  }
  return files;
}
