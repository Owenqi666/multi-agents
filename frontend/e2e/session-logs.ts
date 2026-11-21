import { createHash } from 'node:crypto';
import { expect, type Page } from '@playwright/test';
import type { ArtifactRef, Resource } from '../src/api/types';

type Session = { implementation_attempt: number } | { seat: number; review_attempt: number };

interface RawPage {
  byte_count: number;
  next_offset: number | null;
  offset: number;
  raw_base64: string;
  sha256: string;
}

export async function sessionResource<T>(page: Page, origin: string, kind: 'task' | 'review', id: string) {
  const response = await page.request.get(`${origin}/api/resources/${kind}/${id}?session_logs=1`);
  expect(response.ok()).toBe(true);
  return await response.json() as Resource<T> & { data: T };
}

export function sessionArtifacts(resource: Resource<unknown>, session: Session): ArtifactRef[] {
  const task = 'implementation_attempt' in session;
  const prefix = task ? `session-task-${session.implementation_attempt}-` : `session-review-${session.seat}-${session.review_attempt}-`;
  const artifacts = resource.artifacts.filter((artifact) => artifact.key?.startsWith(prefix));
  expect(artifacts.length, prefix).toBeGreaterThan(0);
  for (const artifact of artifacts) {
    const index = Number(artifact.key!.slice(prefix.length));
    expect(index).toBeGreaterThanOrEqual(0);
    expect(artifact.coordinates).toEqual({ ...session, log_index: index });
  }
  return artifacts;
}

async function rawLog(page: Page, origin: string, artifact: ArtifactRef) {
  expect(artifact.sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(artifact.byte_count).toBeGreaterThan(0);
  const chunks: Buffer[] = [];
  const endpoint = new URL(`/api/artifacts/${artifact.artifact_id}`, origin);
  endpoint.searchParams.set('limit', '4096');
  let offset = 0;
  while (true) {
    endpoint.searchParams.set('offset', String(offset));
    const response = await page.request.get(endpoint.toString());
    expect(response.ok(), endpoint.toString()).toBe(true);
    const data = await response.json() as RawPage;
    expect(data).toMatchObject({ offset, byte_count: artifact.byte_count, sha256: artifact.sha256 });
    expect(data.raw_base64).toMatch(/^[A-Za-z0-9+/]+=*$/);
    const bytes = Buffer.from(data.raw_base64, 'base64');
    expect(bytes.length).toBeGreaterThan(0);
    chunks.push(bytes);
    offset += bytes.length;
    if (data.next_offset === null) break;
    expect(data.next_offset).toBe(offset);
    expect(data.next_offset).toBeLessThan(data.byte_count);
  }
  const all = Buffer.concat(chunks);
  expect(all.length).toBe(artifact.byte_count);
  expect(createHash('sha256').update(all).digest('hex')).toBe(artifact.sha256);
  return { artifact, full: all, pages: chunks.length };
}

export async function checkSessionLog(page: Page, origin: string, artifacts: ArtifactRef[], marker: string, paginate = false) {
  const logs = await Promise.all(artifacts.map((artifact) => rawLog(page, origin, artifact)));
  const log = logs.find((value) => value.full.includes(marker));
  expect(log, `No registered log for ${marker}`).toBeDefined();
  const { artifact, pages } = log!;
  const section = page.locator('section').filter({ has: page.getByRole('heading', { name: '模型会话日志', exact: true }) });
  await section.locator(`a[href^="/logs/${artifact.artifact_id}"]`).click();
  const body = page.locator('.log-body');
  await expect(body).not.toBeEmpty();
  const needsMore = !log!.full.subarray(0, 8192).includes(marker);
  if (paginate) expect(needsMore).toBe(true);
  if (needsMore) {
    await expect(body).not.toContainText(marker);
    expect(pages).toBeGreaterThan(2);
    const more = page.getByRole('button', { name: '读下一页', exact: true });
    for (let index = 0; index < pages; index += 1) {
      await expect(more).toBeVisible();
      const response = page.waitForResponse((value) => {
        const url = new URL(value.url());
        return url.pathname === `/api/artifacts/${artifact.artifact_id}` && Number(url.searchParams.get('offset')) > 0;
      });
      await more.click();
      const next = await response;
      expect(next.ok()).toBe(true);
      if (((await next.json()) as RawPage).next_offset === null) break;
    }
  }
  await expect(body).toContainText(marker);
  await expect(page.locator('.log-hash')).toContainText(artifact.sha256!);
  await page.goBack();
  return { key: artifact.key, coordinates: artifact.coordinates, byte_count: artifact.byte_count, sha256: artifact.sha256, pages };
}
