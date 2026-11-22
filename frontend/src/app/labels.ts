// Words for actions, phases and states, in the interface's voice.

import { getLang, translated } from '../i18n';
export const ACTION_LABEL: Record<string, string> = translated({
  'environment.refresh': '检查环境',
  'project.consult': '需求咨询',
  'project.consult.reconcile': '咨询对账',
  'project.confirm': '确认目标',
  'project.plan': '生成计划',
  'project.revise': '修订计划',
  'project.stage.start': '领取阶段',
  'project.stage.resume': '恢复阶段',
  'project.stage.leaf': '领取子卡',
  'project.stage.integrate': '合并子卡',
  'plan.create': '单卡规划',
  'plan.retry': '重试规划',
  'task.run': '实施',
  'task.retry': '重试实施',
  'task.run_batch': '并行实施子卡',
  'task.test': '测试',
  'task.review': '代码审核',
  'task.retry_review': '补跑审核',
  'task.confirm': '交叉确认',
  'task.rework': '返工',
  'integration.create': '创建集成',
  'integration.test': '集成测试',
  'integration.review': '集成审核',
  'integration.retry_review': '补跑集成审核',
  'integration.confirm': '集成交叉确认',
  'integration.rework': '集成返工',
  'integration.rebase': '重建集成',
  'integration.accept': '接受',
  'review.freeze': '冻结审核输入',
  'review.run': '独立审核',
  'review.retry': '补跑独立审核',
  'review.confirm': '发起交叉确认',
});

export const actionLabel = (action: string): string => ACTION_LABEL[action] ?? action;

export const KIND_LABEL: Record<string, string> = translated({
  project: '项目', consultation: '需求咨询', goal: '目标', 'project-plan': '项目计划', 'project-stages': '阶段列表',
  stage: '阶段', plan: '卡片规划', task: '实施', 'task-test': '测试', 'task-review': '审核', integration: '集成',
  review: '审核轮', snapshot: '冻结快照',
});

export const VERIFICATION_LABEL: Record<string, string> = translated({ verified: '已核验', unverified: '未核验' });

export const ASSESSMENT_LABEL: Record<string, string> = translated({
  ready: '审核通过', needs_rework: '需要返工', user_decision: '需要你判断', incomplete: '审核未完成',
});

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return '—';
  const tail = id.includes('-') ? id.slice(id.lastIndexOf('-') + 1) : id;
  return tail.slice(0, n);
}

export function timeText(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(getLang() === 'en' ? 'en-GB' : 'zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** next_action kinds in words. The backend's own sentence is kept next to it, as written. */
export const NEXT_LABEL: Record<string, string> = translated({
  run: '可以开始实施，或在失败后重试。',
  task_test: '下一步：运行登记的测试命令。',
  task_review: '下一步：选审核席位，开始代码审核。',
  rework: '审核要求返工，可以按阻塞意见返工一次。',
  user_decision: '需要你看过记录再决定下一步。',
  budget_exhausted: '实施预算已经用完。',
  reconciliation_required: '记录不完整或没核验，先看保留的证据，不会自动恢复。',
  plan_retry: '规划没有完成，可以恢复阶段重新规划。',
  project_stage: '拆分已核验，去领取子卡。',
});

export function nextText(next: { kind: string; detail?: string } | null | undefined): string | null {
  if (!next) return null;
  return NEXT_LABEL[next.kind] ?? next.detail ?? null;
}
