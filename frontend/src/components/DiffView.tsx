import { Fragment, useEffect, useRef, type ReactNode } from 'react';
import type { Finding } from '../api/types';
import { parseDiff, type DiffFile, type DiffLine } from '../diff/parse';
import { Badge } from './Badge';

const STATUS: Record<DiffFile['status'], string> = {
  modified: '改动', added: '新增', deleted: '删除', renamed: '重命名', copied: '复制',
};

/** A review finding placed on the diff. anchor says which side of this diff the finding's
 *  lines number: the new side for the reviewed diff itself, the old side for a rework
 *  increment, whose old side is the candidate that was reviewed. */
export interface Annotation {
  finding: Finding;
  seat: string;
  anchor: 'old' | 'new';
}

export interface PatchLocation {
  side: 'baseline' | 'candidate';
  path: string;
  start: number;
  end: number;
}

function lineKey(anchor: 'old' | 'new', path: string | null, line: DiffLine): string | null {
  const n = anchor === 'new' ? line.new : line.old;
  return path && n !== null ? `${anchor}:${path}:${n}` : null;
}

/** One finding under the line it ends on. */
function FindingRow({ a, onOpen }: { a: Annotation; onOpen?: (a: Annotation) => void }) {
  const f = a.finding;
  const blocking = f.severity === 'P0' || f.severity === 'P1';
  return (
    <div className={blocking ? 'finding blocking' : 'finding'} role="note">
      <div className="finding-head">
        <Badge tone={blocking ? 'fail' : 'warn'}>{blocking ? `阻塞 ${f.severity}` : f.severity}</Badge>
        <span className="finding-title">{f.title}</span>
        <span className="finding-meta">{a.seat}</span>
      </div>
      <p className="finding-body">{f.impact}</p>
      <div className="finding-foot">
        <span className="finding-loc">{f.path}:{f.line_start}{f.line_end !== f.line_start ? `–${f.line_end}` : ''}</span>
        {f.reference_status === 'unverified' ? <span className="finding-ref">引用没有对上冻结文件</span> : null}
        {onOpen ? <button type="button" className="link" onClick={() => onOpen(a)}>在冻结文件里查看</button> : null}
      </div>
    </div>
  );
}

/** A unified diff rendered as text. Nothing from the diff is ever treated as markup. */
export function DiffView({ diff, annotations = [], onOpenFinding, emptyNote, location }: {
  diff: string | null;
  annotations?: Annotation[];
  onOpenFinding?: (a: Annotation) => void;
  emptyNote?: ReactNode;
  location?: PatchLocation;
}) {
  const target = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    if (target.current) {
      target.current.scrollIntoView?.({ block: 'center' });
      target.current.focus({ preventScroll: true });
    }
  }, [diff, location]);
  if (diff === null) return <>{emptyNote}</>;
  const files = parseDiff(diff);
  const targetOnNew = location?.side === 'candidate';
  let targetFound = false;
  const byLine = new Map<string, Annotation[]>();
  for (const a of annotations) {
    const key = `${a.anchor}:${a.finding.path}:${a.finding.line_end}`;
    byLine.set(key, [...(byLine.get(key) ?? []), a]);
  }
  const marked = new Set<string>();
  for (const a of annotations) {
    for (let n = a.finding.line_start; n <= a.finding.line_end; n++) marked.add(`${a.anchor}:${a.finding.path}:${n}`);
  }
  return (
    <div className="diff">
      {files.map((f, fi) => (
        <section className="diff-file" key={fi}>
          <header className="diff-file-head">
            <span className="diff-status" data-status={f.status}>{STATUS[f.status]}</span>
            <span className="diff-path">
              {f.status === 'renamed' || f.status === 'copied'
                ? <>{f.oldPath}<span className="arrow"> → </span>{f.newPath}</>
                : (f.newPath ?? f.oldPath)}
            </span>
            {f.similarity !== null ? <span className="diff-meta">相似度 {f.similarity}%</span> : null}
            {f.modeChange ? <span className="diff-meta">权限 {f.modeChange}</span> : null}
          </header>
          {f.binary ? <p className="diff-binary">二进制文件，不显示内容。</p> : null}
          {!f.binary && !f.hunks.length ? <p className="diff-binary">只有文件名或权限变化。</p> : null}
          {f.hunks.length ? (
            <div className="diff-body">
              <table className="diff-table">
                <tbody>
                  {f.hunks.map((h, hi) => (
                    <Fragment key={hi}>
                      <tr className="diff-hunk"><td colSpan={4}>{h.header}</td></tr>
                      {h.lines.map((l, li) => {
                        const kc = lineKey('new', f.newPath, l);
                        const kb = lineKey('old', f.oldPath, l);
                        const notes = [...(kc ? byLine.get(kc) ?? [] : []), ...(kb ? byLine.get(kb) ?? [] : [])];
                        const selectedPath = targetOnNew ? f.newPath : f.oldPath;
                        const selectedLine = targetOnNew ? l.new : l.old;
                        const inRange = !!location && location.path === selectedPath && selectedLine !== null && selectedLine >= location.start && selectedLine <= location.end;
                        const firstInRange = inRange && !targetFound;
                        if (firstInRange) targetFound = true;
                        const hot = inRange || (kc && marked.has(kc)) || (kb && marked.has(kb));
                        return (
                          <Fragment key={li}>
                            <tr className={`dl dl-${l.kind}${hot ? ' hot' : ''}`} id={kc ? `L-${kc}` : undefined}
                              ref={firstInRange ? target : undefined} tabIndex={firstInRange ? -1 : undefined} aria-current={firstInRange ? 'location' : undefined}>
                              <td className="no">{l.old ?? ''}</td>
                              <td className="no">{l.new ?? ''}</td>
                              <td className="mk" aria-hidden="true">{l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : ''}</td>
                              <td className="tx">{l.kind === 'note' ? `\\ ${l.text}` : l.text}</td>
                            </tr>
                            {notes.length ? (
                              <tr className="dl-notes"><td colSpan={4}>{notes.map((a) => <FindingRow key={a.finding.id} a={a} onOpen={onOpenFinding} />)}</td></tr>
                            ) : null}
                          </Fragment>
                        );
                      })}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      ))}
      {location && !targetFound ? <p className="muted">补丁中没有 {location.path}:{location.start} 对应的行。</p> : null}
    </div>
  );
}
