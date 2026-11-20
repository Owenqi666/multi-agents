import { describe, expect, it } from 'vitest';
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
});
