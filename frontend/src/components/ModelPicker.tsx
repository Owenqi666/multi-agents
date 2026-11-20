import { useEffect, useMemo } from 'react';
import type { Candidate, RoleRequest, Workspace } from '../api/types';

/** Families the web offers at all; Luna and Haiku never appear even if a catalog lists them. */
export const ALLOWED_FAMILIES = new Set(['Astra', 'Sol', 'Terra', 'Fable', 'Opus', 'Sonnet']);
const FALLBACK_EFFORTS: Record<string, string[]> = {
  codex: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  claude: ['low', 'medium', 'high', 'xhigh', 'max'],
};

export type RoleId = 'advisor' | 'planner' | 'stage_lead' | 'implementation' | 'fallback' | 'review';

export interface PickerChoice {
  model: string;
  effort: string;
}

/** Candidates for a role, filtered by the contract's rules. `only` narrows the families
 *  (leaf executors: Terra or Sonnet); `exclude` drops a model (card review cannot be Sol's). */
export function candidatesFor(ws: Workspace | null, role: RoleId, only?: string[], exclude?: string | null): Candidate[] {
  const r = ws?.presets.roles.find((x) => x.id === role);
  return (r?.candidates ?? []).filter((c) =>
    ALLOWED_FAMILIES.has(c.family) && (!only || only.includes(c.family)) && c.model !== exclude);
}

export function effortsFor(c: Candidate | undefined): string[] {
  if (!c) return [];
  return c.efforts && c.efforts.length ? c.efforts : FALLBACK_EFFORTS[c.provider] ?? [];
}

/** The request the action expects. Implementation, fallback and leaf executors use the
 *  dispatch provider name claude-code; the others use claude. */
export function toRequest(c: Candidate, effort: string, role: RoleId): RoleRequest {
  const dispatch = role === 'implementation' || role === 'fallback';
  const provider = dispatch && c.provider === 'claude' ? 'claude-code' : c.provider;
  return { provider, model: c.model, effort };
}

export function ModelPicker({ ws, role, value, onChange, only, exclude, label }: {
  ws: Workspace | null;
  role: RoleId;
  value: PickerChoice | null;
  onChange: (v: PickerChoice | null) => void;
  only?: string[];
  exclude?: string | null;
  label: string;
}) {
  const list = useMemo(() => candidatesFor(ws, role, only, exclude), [ws, role, only, exclude]);
  const current = list.find((c) => c.model === value?.model);
  const efforts = effortsFor(current);

  useEffect(() => {
    if (!list.length) { if (value) onChange(null); return; }
    if (!current) {
      const c = list[0]!;
      const e = effortsFor(c);
      onChange({ model: c.model, effort: e.includes('high') ? 'high' : e[0] ?? 'high' });
    } else if (value && !efforts.includes(value.effort) && efforts.length) {
      onChange({ model: current.model, effort: efforts.includes('high') ? 'high' : efforts[0]! });
    }
  }, [list, current, efforts, value, onChange]);

  if (!list.length) {
    return <div className="picker empty-picker"><span className="picker-label">{label}</span><span className="action-why">没有可选的模型。Codex 的候选要先检查一次环境才会出现。</span></div>;
  }
  return (
    <div className="picker">
      <label className="picker-label">{label}</label>
      <select aria-label={`${label}：模型`} value={value?.model ?? ''} onChange={(e) => {
        const c = list.find((x) => x.model === e.target.value);
        if (c) { const ef = effortsFor(c); onChange({ model: c.model, effort: value && ef.includes(value.effort) ? value.effort : ef.includes('high') ? 'high' : ef[0] ?? 'high' }); }
      }}>
        {list.map((c) => <option key={c.model} value={c.model}>{c.family} · {c.model}</option>)}
      </select>
      <select aria-label={`${label}：档位`} value={value?.effort ?? ''} onChange={(e) => value && onChange({ model: value.model, effort: e.target.value })}>
        {efforts.map((e) => <option key={e} value={e}>{e}</option>)}
      </select>
    </div>
  );
}

export function requestFrom(ws: Workspace | null, role: RoleId, v: PickerChoice | null): RoleRequest | null {
  if (!v) return null;
  const c = candidatesFor(ws, role).find((x) => x.model === v.model);
  return c ? toRequest(c, v.effort, role) : null;
}
