import type { Job, RunItem, Stage, TaskView } from '../api/types';
import type { TimelineIntegration } from './integrationLineage';

/** A plan can contain multiple stages with independent runs. Attribute active work
 * to exact stage, task and round IDs, using the navigation registry on fresh loads. */
export function belongsToStage(
  job: Pick<Job, 'action' | 'args'>,
  stageId: string,
  stage: Stage | null,
  history: readonly TimelineIntegration[],
  runs: readonly RunItem[],
): boolean {
  const args = job.args ?? {};
  if (typeof args.stage_run_id === 'string') return args.stage_run_id === stageId;
  if (job.action === 'project.stage.start') {
    const registered = runs.find((run) => run.kind === 'stage' && run.id === stageId);
    const planId = stage?.plan_id ?? registered?.parent_id;
    const slug = stage?.stage_id ?? registered?.label;
    return !!planId && !!slug && args.plan_id === planId && args.stage_id === slug;
  }

  const known = new Set([stageId]);
  const add = (id: string | null | undefined) => { if (id) known.add(id); };
  const addTask = (task: TaskView | null | undefined) => {
    for (const round of task?.reviews ?? []) {
      add(round.round_id);
      add(round.confirmation_round_id);
    }
  };
  add(stage?.planning_run_id);
  if (stage?.mode === 'direct') {
    add(stage.task_run_id);
    addTask(stage.implementation);
  } else if (stage?.mode === 'delegate') {
    add(stage.integration?.integration_id);
    for (const integration of history) add(integration.integration_id);
    for (const leaf of stage.leaves) {
      add(leaf.planning_run_id);
      add(leaf.task_run_id);
      addTask(leaf.implementation);
      for (const previous of leaf.prior_planning_run_ids ?? []) add(previous);
    }
  }

  const parents = new Map(runs.map((run) => [run.id, run.parent_id]));
  const owns = (id: unknown): boolean => {
    const visited = new Set<string>();
    while (typeof id === 'string' && !visited.has(id)) {
      if (known.has(id)) return true;
      visited.add(id);
      id = parents.get(id);
    }
    return false;
  };
  const ids = args.planning_run_ids ?? args.task_run_ids;
  if (Array.isArray(ids)) return ids.length > 0 && ids.every(owns);
  const owner = args.planning_run_id ?? args.task_run_id ?? args.integration_id ?? args.round_id;
  return owns(owner);
}
