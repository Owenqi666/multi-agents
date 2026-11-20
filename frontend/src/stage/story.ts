import type { Family, Tone } from './models';

/** One holder of the work at a station. A dual review or a pair of parallel leaves gives a
 *  station two holders, each on its own branch of the line. */
export interface Holder {
  /** Stable within a station, e.g. "reviewer-1" or a leaf card id. */
  key: string;
  family: Family;
  tone: Tone;
  /** Station text: reported model short name, 未确认, 你 or 工作台. */
  name: string;
  /** Second station line: reported effort, 档位未确认, or the request marked 请求. */
  sub: string;
  /** What this seat is, in words: 顾问 1, reviewer-1, 子卡 A. */
  role: string;
  reportedModel: string | null;
  reportedEffort: string | null;
  requestedModel: string | null;
  requestedEffort: string | null;
  /** True for 你 and 工作台, which have no model evidence. */
  plain: boolean;
}

/** blind: dual review, the two seats cannot see each other. parallel: leaves run side by side. */
export type Fork = 'blind' | 'parallel' | null;

export interface Beat {
  id: string;
  /** Row of the 12-step table in the contract ("接力线的步骤和数据来源"). */
  step: number;
  title: string;
  phase: string;
  holders: Holder[];
  fork: Fork;
  /** The handoff object carried into this station; null when nothing is handed over. */
  carry: string | null;
  note: string;
}

export interface Span {
  from: number;
  to: number;
  label: string;
}

export interface Story {
  beats: Beat[];
  /** Phase ruler above the line. */
  phases: Span[];
  /** A rework loop: from the first test to the rework station that sends the line back. */
  loops: Span[];
}
