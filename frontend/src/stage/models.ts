// Model names as the stage shows them. The text on screen only ever comes from a value
// the runtime reported; requested values are shown separately and marked 请求.

import { tx } from '../i18n';
export type Family = 'claude' | 'gpt' | 'human' | 'system';
export type Tone = 'fable' | 'opus' | 'sonnet' | 'astra' | 'sol' | 'terra' | 'you' | 'bench' | 'unknown';

export interface ParsedModel {
  family: 'claude' | 'gpt';
  tone: Tone;
  /** Short station label, such as "Sol" or "Opus". */
  short: string;
  /** Full caption label, such as "GPT-6.2 Sol" or "Claude Opus 5.5". */
  full: string;
}

const CLAUDE = /^claude-(fable|opus|sonnet)-(\d+)(?:-(\d+))?$/;
const GPT = /^gpt-(\d+(?:\.\d+)?)-(astra|sol|terra)$/;
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** Recognise the model id formats the bridge accepts; anything else is shown verbatim. */
export function parseModel(id: string | null | undefined): ParsedModel | null {
  if (!id) return null;
  const c = CLAUDE.exec(id);
  if (c) {
    const name = c[1] as 'fable' | 'opus' | 'sonnet';
    const version = c[3] ? `${c[2]}.${c[3]}` : c[2];
    return { family: 'claude', tone: name, short: cap(name), full: `Claude ${cap(name)} ${version}` };
  }
  const g = GPT.exec(id);
  if (g) {
    const name = g[2] as 'astra' | 'sol' | 'terra';
    return { family: 'gpt', tone: name, short: cap(name), full: `GPT-${g[1]} ${cap(name)}` };
  }
  return null;
}

export const UNCONFIRMED = '未确认';

/** Station text for a reported model: its short name, the raw string when it does not
 *  parse (a fake runtime reports placeholders), or 未确认 when nothing was reported. */
export function reportedShort(reported: string | null): string {
  if (reported === null) return tx(UNCONFIRMED);
  return parseModel(reported)?.short ?? reported;
}

export function reportedFull(reported: string | null): string {
  if (reported === null) return tx(UNCONFIRMED);
  return parseModel(reported)?.full ?? reported;
}

/** Colour follows the seat a holder was given: the reported model when it parses, else the
 *  requested one, else the provider. Colour is an identity aid only, never a model claim. */
export function toneFor(reported: string | null, requested: string | null, provider: string | null): Tone {
  const parsed = parseModel(reported) ?? parseModel(requested);
  if (parsed) return parsed.tone;
  if (provider === 'codex') return 'sol';
  if (provider === 'claude' || provider === 'claude-code') return 'opus';
  return 'unknown';
}

export function familyOfTone(tone: Tone): Family {
  if (tone === 'fable' || tone === 'opus' || tone === 'sonnet') return 'claude';
  if (tone === 'astra' || tone === 'sol' || tone === 'terra') return 'gpt';
  if (tone === 'you') return 'human';
  return 'system';
}

export const toneVar = (tone: Tone): string => `var(--tone-${tone})`;
