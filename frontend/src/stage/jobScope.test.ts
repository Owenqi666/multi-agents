import { expect, it } from 'vitest';
import { delegateStage, directStage } from '../fixtures/specimen';
import { belongsToProjectTimeline, belongsToStage } from './jobScope';

const projectTimeline = { projectId: 'project-a', goalId: 'goal-a', planId: 'plan-a', consultationId: 'consultation-a', empty: false };

it('matches planning and revision by their exact goal or plan', () => {
  expect(belongsToProjectTimeline({ action: 'project.plan', args: { goal_id: 'goal-a' } }, projectTimeline)).toBe(true);
  expect(belongsToProjectTimeline({ action: 'project.plan', args: { goal_id: 'another-goal' } }, projectTimeline)).toBe(false);
  expect(belongsToProjectTimeline({ action: 'project.revise', args: { plan_id: 'plan-a' } }, projectTimeline)).toBe(true);
  expect(belongsToProjectTimeline({ action: 'project.revise', args: { plan_id: 'another-plan' } }, projectTimeline)).toBe(false);
});

it('matches consultation and confirmation by the available project or consultation ID', () => {
  expect(belongsToProjectTimeline({ action: 'project.consult', args: { project_id: 'project-a' } }, projectTimeline)).toBe(true);
  expect(belongsToProjectTimeline({ action: 'project.consult', args: { project_id: 'project-b' } }, projectTimeline)).toBe(false);
  expect(belongsToProjectTimeline({ action: 'project.confirm', args: { project_id: 'project-a' } }, projectTimeline)).toBe(true);
  const confirm = { action: 'project.confirm', args: { consultation_id: 'consultation-a' } };
  expect(belongsToProjectTimeline(confirm, { ...projectTimeline, projectId: null })).toBe(true);
  expect(belongsToProjectTimeline({ ...confirm, args: { consultation_id: 'another-consultation', project_id: 'project-a' } }, projectTimeline)).toBe(false);
  expect(belongsToProjectTimeline({ ...confirm, args: { ...confirm.args, project_id: 'project-b' } }, projectTimeline)).toBe(false);
});

it('shows a new project on an empty timeline with a recorded submission', () => {
  const empty = { projectId: null, goalId: null, planId: null, consultationId: null, empty: true };
  const consult = { action: 'project.consult', args: { original_goal: { length: 8 } } };
  expect(belongsToProjectTimeline(consult, empty)).toBe(true);
  expect(belongsToProjectTimeline({ ...consult, action: 'project.confirm' }, empty)).toBe(true);
  expect(belongsToProjectTimeline(consult, { ...empty, empty: false })).toBe(false);
  expect(belongsToProjectTimeline(consult, projectTimeline)).toBe(false);
  expect(belongsToProjectTimeline({ ...consult, args: null }, empty)).toBe(false);
});

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
