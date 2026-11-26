// The unsubmitted goal text, kept in this browser per repository. It comes back after leaving
// the page, a refresh or a language switch, and is removed once the service has taken that
// very text. Storage may be unavailable; the form then simply starts empty.

const PREFIX = 'multi-agent.goal-draft:';

export interface GoalDraft {
  goal: string;
  saved_at: string;
}

/** One draft per repository; a form opened from a consultation keeps its edits apart. */
export function draftKey(repository: string, consultation?: string | null): string {
  return `${PREFIX}${repository}${consultation ? `#consultation=${consultation}` : ''}`;
}

export function loadDraft(key: string): GoalDraft | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(key) ?? 'null');
    if (value && typeof value === 'object' && typeof (value as GoalDraft).goal === 'string' && (value as GoalDraft).goal.trim()) {
      return { goal: (value as GoalDraft).goal, saved_at: String((value as GoalDraft).saved_at ?? '') };
    }
  } catch {
    /* unreadable storage or an old value: no draft */
  }
  return null;
}

export function saveDraft(key: string, goal: string): void {
  try {
    if (goal.trim()) window.localStorage.setItem(key, JSON.stringify({ goal, saved_at: new Date().toISOString() } satisfies GoalDraft));
    else window.localStorage.removeItem(key);
  } catch {
    /* the draft then lasts for this page only */
  }
}

/** Remove the stored draft only if it still is the text that was submitted. Another tab, or
 *  an edit made while the job ran, may have stored a newer one, which stays. */
export function clearSubmitted(key: string, submitted: string): void {
  try {
    if (loadDraft(key)?.goal === submitted) window.localStorage.removeItem(key);
  } catch {
    /* nothing stored */
  }
}

/** A draft that has been sent and whose job has not been seen to end. Kept so that a page
 *  reloaded while the job runs can still drop the draft once the service has taken the text. */
export interface SentDraft {
  job_id: string;
  goal: string;
}

const sentKey = (key: string): string => `${key}#sent`;

export function rememberSent(key: string, sent: SentDraft): void {
  try {
    window.localStorage.setItem(sentKey(key), JSON.stringify(sent));
  } catch {
    /* without it the draft simply stays after a reload */
  }
}

export function loadSent(key: string): SentDraft | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(sentKey(key)) ?? 'null');
    if (value && typeof value === 'object' && typeof (value as SentDraft).job_id === 'string' && typeof (value as SentDraft).goal === 'string') {
      return { job_id: (value as SentDraft).job_id, goal: (value as SentDraft).goal };
    }
  } catch {
    /* unreadable: nothing was sent as far as this page knows */
  }
  return null;
}

export function forgetSent(key: string): void {
  try {
    window.localStorage.removeItem(sentKey(key));
  } catch {
    /* nothing stored */
  }
}
