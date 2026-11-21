import { describe, expect, it } from 'vitest';
import type { StageIntegration } from '../api/types';
import { collabHistory, collabStage, delegateStage, directStage, consultation, goal, plan } from '../fixtures/specimen';
import { deriveStory } from './derive';

describe('deriveStory', () => {
  const direct = deriveStory({ consultation, goal, plan, stage: directStage });

  it('draws the direct stage with one rework loop back to testing', () => {
    expect(direct.beats.map((b) => b.title)).toEqual([
      '需求顾问', '需求顾问', '确认目标', '项目计划', '领取阶段', '拟卡', '卡片复核', '实施',
      '测试', '代码审核', '返工', '复测', '复审',
    ]);
    expect(direct.loops).toEqual([{ from: 8, to: 10, label: '返工后回到测试' }]);
  });

  it('splits dual review into two blind branches', () => {
    const review = direct.beats[9]!;
    expect(review.fork).toBe('blind');
    expect(review.holders.map((h) => h.key)).toEqual(['reviewer-1', 'reviewer-2']);
  });

  it('shows only reported models and marks a missing report 未确认', () => {
    const [opus, astra] = direct.beats[9]!.holders;
    expect(opus!.name).toBe('Opus');
    expect(opus!.sub).toBe('high');
    expect(astra!.name).toBe('未确认');
    expect(astra!.reportedModel).toBeNull();
    expect(astra!.requestedModel).toBe('gpt-6.2-astra');
    expect(astra!.sub).toBe('请求 Astra');
  });

  it('keeps a single seat on one line and draws no loop without rework', () => {
    const delegate = deriveStory({ stage: delegateStage });
    expect(delegate.beats.map((b) => b.title)).toEqual(['领取阶段', '拆分', '卡片复核', '实施', '合并', '测试', '代码审核', '接受']);
    expect(delegate.beats[6]!.fork).toBeNull();
    expect(delegate.beats[6]!.holders).toHaveLength(1);
    expect(delegate.beats[3]!.fork).toBe('parallel');
    expect(delegate.beats[3]!.holders.map((h) => h.name)).toEqual(['Terra', 'Sonnet']);
    expect(delegate.loops).toEqual([]);
  });

  it('draws only the steps that have records', () => {
    expect(deriveStory({}).beats).toEqual([]);
    expect(deriveStory({ goal }).beats.map((b) => b.title)).toEqual(['确认目标']);
  });

  it('follows a delegate rework into the child integration', () => {
    const story = deriveStory({ stage: collabStage, integrationHistory: collabHistory });
    expect(story.beats.map((b) => b.title)).toEqual([
      '领取阶段', '拆分', '卡片复核', '实施', '合并', '测试', '代码审核', '返工', '复测', '复审', '接受',
    ]);
    expect(story.loops).toEqual([{ from: 5, to: 7, label: '返工后回到测试' }]);
    const rework = story.beats[7]!.holders[0]!;
    expect(rework.tone).toBe('terra');
    expect(rework.name).toBe('未确认');
    expect(story.beats[6]!.holders.map((h) => h.tone)).toEqual(['opus', 'astra']);
  });

  it('uses the rework source round and does not draw the inherited reservation again in the child', () => {
    const parent = structuredClone(collabHistory[0]!) as StageIntegration;
    const child = structuredClone(collabStage.integration!);
    const rework = parent.evidence!.rework!;
    const blockedReview = parent.evidence!.reviews[0]!;
    rework.request = { ...rework.request, review_round_id: blockedReview.round_id, findings: [{}] };
    rework.result = { status: 'succeeded', evidence: { reported_model: 'gpt-6.2-terra', reported_effort: null } };
    child.evidence!.rework = rework;
    parent.evidence!.reviews.push(child.evidence!.reviews[0]!);

    const story = deriveStory({ stage: { ...collabStage, integration: child }, integrationHistory: [parent] });
    const [, initial, repair, retest, final, accept] = story.beats.slice(5);
    expect(story.beats.slice(5).map((beat) => beat.title)).toEqual(['测试', '代码审核', '返工', '复测', '复审', '接受']);
    expect(initial?.note).toContain('候选 b7c6d5e。结论：需要返工，1 条阻塞意见。');
    expect(repair?.carry).toBe('1 条阻塞意见');
    expect(repair?.holders[0]?.reportedModel).toBe('gpt-6.2-terra');
    expect(repair?.holders[0]?.reportedEffort).toBeNull();
    expect(retest?.carry).toBe('修订候选 d4e5f60');
    expect(final?.note).toContain('候选 d4e5f60。结论：通过。');
    expect(accept?.carry).toBe('候选 d4e5f60');
  });

  it('does not show a previous review as the verdict for a new test attempt', () => {
    const integration = structuredClone(delegateStage.integration!);
    integration.evidence!.test!.attempt = 2;
    integration.evidence!.reviews[0]!.test_attempt = 1;
    integration.evidence!.acceptance = null;
    const story = deriveStory({ stage: { ...delegateStage, integration } });
    expect(story.beats.slice(-2).map((beat) => beat.title)).toEqual(['合并', '测试']);
  });
});
