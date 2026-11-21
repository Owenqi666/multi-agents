import { expect, it } from 'vitest';
import { delegateStage, directStage } from '../fixtures/specimen';
import { belongsToStage } from './jobScope';

it('attributes leaf implementation to its own run, even when two stages share a project plan', () => {
  const leaf = delegateStage.leaves[0]!;
  const job = { action: 'task.run', args: { planning_run_id: leaf.planning_run_id } };

  expect(delegateStage.plan_id).toBe(directStage.plan_id);
  expect(belongsToStage(job, delegateStage.stage_run_id, delegateStage, [], [])).toBe(true);
  expect(belongsToStage(job, directStage.stage_run_id, directStage, [], [])).toBe(false);
});

it('follows registered review ownership when the stage read is locked', () => {
  const roundId = 'task-review-1';
  const taskId = 'task-1';
  const planId = 'leaf-plan-1';
  const runs = [
    { kind: 'task-review', id: roundId, parent_id: taskId, label: '', updated_at: '' },
    { kind: 'task', id: taskId, parent_id: planId, label: '', updated_at: '' },
    { kind: 'plan', id: planId, parent_id: delegateStage.stage_run_id, label: '', updated_at: '' },
  ];
  const job = { action: 'task.rework', args: { round_id: roundId } };
  expect(belongsToStage(job, delegateStage.stage_run_id, null, [], runs)).toBe(true);
  expect(belongsToStage(job, directStage.stage_run_id, null, [], runs)).toBe(false);
});
