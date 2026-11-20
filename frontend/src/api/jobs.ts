// One place that knows about jobs: which CLI job is unfinished, polling it every second,
// finding it again after a page refresh, submitting with duplicate protection, cancelling.
// Pages read `generation` to re-read their records once after a job ends.

import { useSyncExternalStore } from 'react';
import { api } from './client';
import { ApiError } from './transport';
import { isUnfinished, type Job } from './types';

export const ENVIRONMENT_ACTION = 'environment.refresh';
const isCli = (job: Pick<Job, 'action'>): boolean => job.action !== ENVIRONMENT_ACTION;

export interface JobCenterState {
  /** The unfinished CLI job, if any. Environment checks never hold the repository lock. */
  active: Job | null;
  /** Unfinished environment checks, by provider. */
  environment: Record<string, Job>;
  /** Recent jobs, newest first, as last read from GET /api/jobs. */
  recent: Job[];
  /** Bumps whenever a job finishes, so pages read their records once more. */
  generation: number;
  /** An action that was clicked and has not got a job id back yet. */
  submitting: string | null;
  /** The first GET /api/jobs has answered. */
  ready: boolean;
}

export type SubmitResult = { job: Job } | { busy: string | null };

const POLL_MS = 1000;

export class JobCenter {
  private state: JobCenterState = { active: null, environment: {}, recent: [], generation: 0, submitting: null, ready: false };
  private listeners = new Set<() => void>();
  private watched = new Map<string, Job>();
  private waiters = new Map<string, ((job: Job) => void)[]>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getState = (): JobCenterState => this.state;

  /** Tests only: forget every job and timer. */
  reset(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.watched.clear();
    this.waiters.clear();
    this.set({ active: null, environment: {}, recent: [], generation: 0, submitting: null, ready: false });
  }

  private set(patch: Partial<JobCenterState>): void {
    this.state = { ...this.state, ...patch };
    for (const l of this.listeners) l();
  }

  /** After a page load or refresh: find unfinished jobs again from the job list. */
  async init(): Promise<void> {
    try {
      const { items } = await api.jobs();
      this.set({ recent: items, ready: true });
      for (const job of items) if (isUnfinished(job)) this.watch(job);
    } catch {
      this.set({ ready: true });
    }
  }

  async refreshRecent(): Promise<void> {
    try {
      const { items } = await api.jobs();
      this.set({ recent: items });
    } catch {
      /* the list is a convenience; the watched jobs carry the state */
    }
  }

  /** True while no CLI job is unfinished and nothing is being submitted. */
  get idle(): boolean {
    return this.state.active === null && this.state.submitting === null;
  }

  /** Submit an action. While a CLI job is unfinished or a click is still on its way, nothing
   *  is sent: the page points at the running job instead. A 409 busy from the bridge says
   *  the same with the id of the job that holds it. */
  async submit(action: string, args: Record<string, unknown>): Promise<SubmitResult> {
    const cli = action !== ENVIRONMENT_ACTION;
    if (cli && (this.state.active || this.state.submitting)) return { busy: this.state.active?.job_id ?? null };
    if (cli) this.set({ submitting: action });
    try {
      const receipt = await api.submit(action, args);
      const job: Job = { job_id: receipt.job_id, action, state: receipt.state as Job['state'], exit_code: null, result: null, error: null, args };
      this.watch(job);
      return { job };
    } catch (e) {
      if (e instanceof ApiError && e.code === 'busy') {
        const id = (e.details?.active_job_id as string | undefined) ?? null;
        if (id) void this.track(id);
        return { busy: id };
      }
      throw e;
    } finally {
      if (cli) this.set({ submitting: null });
    }
  }

  async cancel(jobId: string): Promise<void> {
    await api.cancel(jobId);
    await this.pollOnce(jobId);
  }

  /** Resolves with the job once it is no longer queued, running or cancelling. */
  waitFor(jobId: string): Promise<Job> {
    const known = this.watched.get(jobId);
    if (known && !isUnfinished(known)) return Promise.resolve(known);
    return new Promise((resolve) => this.waiters.set(jobId, [...(this.waiters.get(jobId) ?? []), resolve]));
  }

  /** Start following a job we only know the id of (from a 409 busy). */
  async track(jobId: string): Promise<void> {
    if (this.watched.has(jobId)) return;
    try {
      this.watch(await api.job(jobId));
    } catch {
      /* the job list will show it */
    }
  }

  private watch(job: Job): void {
    this.watched.set(job.job_id, job);
    this.publish(job);
    this.schedule();
  }

  private publish(job: Job): void {
    const unfinished = isUnfinished(job);
    const patch: Partial<JobCenterState> = {};
    if (isCli(job)) {
      if (unfinished) patch.active = job;
      else if (this.state.active?.job_id === job.job_id) patch.active = null;
    } else {
      const provider = String((job.args as { provider?: string } | null | undefined)?.provider ?? job.job_id);
      const environment = { ...this.state.environment };
      if (unfinished) environment[provider] = job;
      else delete environment[provider];
      patch.environment = environment;
    }
    const recent = [job, ...this.state.recent.filter((j) => j.job_id !== job.job_id)];
    patch.recent = recent;
    if (!unfinished) {
      patch.generation = this.state.generation + 1;
      this.watched.delete(job.job_id);
      for (const resolve of this.waiters.get(job.job_id) ?? []) resolve(job);
      this.waiters.delete(job.job_id);
    }
    this.set(patch);
  }

  private schedule(): void {
    if (this.timer || !this.watched.size) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void Promise.all([...this.watched.keys()].map((id) => this.pollOnce(id))).finally(() => this.schedule());
    }, POLL_MS);
  }

  private async pollOnce(jobId: string): Promise<void> {
    try {
      const job = await api.job(jobId);
      const before = this.watched.get(jobId);
      if (!before || before.state !== job.state || !isUnfinished(job)) this.publish(job);
      else this.watched.set(jobId, job);
    } catch {
      /* keep polling; the bridge may be restarting */
    }
  }
}

export const jobs = new JobCenter();

export function useJobs(): JobCenterState {
  return useSyncExternalStore(jobs.subscribe, jobs.getState);
}
