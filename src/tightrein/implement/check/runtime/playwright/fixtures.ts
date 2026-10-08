// 工作区用例从 tightrein 的 implement/check/runtime/playwright(经 e2e/tsconfig.json 的 paths 映射到本文件)导入 test 与 expect。
// 自动 fixture observe 在每条用例开始时订阅页面事件，结束时把观察结果以 JSON 附件 tightrein-observations 挂到用例结果上：
// 控制台报错、失败请求(requestfailed 与状态码 >= 400 的响应，计划文件 ignoreRequests 中列出的除外)、整页加载耗时、访问过的页面。
import * as fs from 'fs';
import { test as base, expect, type Page, type Request } from '@playwright/test';

export const OBSERVATIONS = 'tightrein-observations';
const FAILED_STATUS = 400;

interface IgnoredRequest {
  method: string;
  pathPattern: string;
  status: number;
}

interface Observations {
  consoleErrors: { time: string; pageUrl: string; text: string; source: string | null }[];
  failedRequests: {
    time: string;
    pageUrl: string;
    method: string;
    url: string;
    status: number | null;
    failure: string | null;
    durationMs: number | null;
  }[];
  loads: { pageUrl: string; durationMs: number }[];
  pages: string[];
}

let ignored: IgnoredRequest[] | undefined;

function ignoredRequests(): IgnoredRequest[] {
  if (ignored === undefined) {
    const planPath = process.env.TIGHTREIN_PAGE_PLAN;
    ignored = planPath ? JSON.parse(fs.readFileSync(planPath, 'utf-8')).ignoreRequests ?? [] : [];
  }
  return ignored as IgnoredRequest[];
}

function isIgnored(method: string, url: string, status: number): boolean {
  const pathname = new URL(url).pathname;
  return ignoredRequests().some(
    (item) => item.method === method && item.status === status && new RegExp(item.pathPattern).test(pathname),
  );
}

const NETWORK_IDLE_TIMEOUT_MS = 3000;

// 截图防抖(38-external-techniques.md 第 6 项)：截图前冻结动画与过渡、隐藏光标，等待网络空闲与字体加载完成。
export const STABILIZE_STYLE = `
*, *::before, *::after {
  animation-delay: -1ms !important;
  animation-duration: 1ms !important;
  animation-iteration-count: 1 !important;
  background-attachment: initial !important;
  caret-color: transparent !important;
  transition-delay: 0s !important;
  transition-duration: 0s !important;
}
`;

// 三步都是尽力而为：页面有轮询请求时等不到网络空闲、CSP 禁止内联样式时注入失败，都照常截图，不让防抖本身造成用例失败。
export async function stabilize(page: Page): Promise<void> {
  await page.addStyleTag({ content: STABILIZE_STYLE }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: NETWORK_IDLE_TIMEOUT_MS }).catch(() => undefined);
  await page.evaluate(() => document.fonts.ready.then(() => undefined)).catch(() => undefined);
}

export const test = base.extend<{ observe: void }>({
  page: async ({ page }, use) => {
    const screenshot = page.screenshot.bind(page);
    page.screenshot = async (options?: Parameters<Page['screenshot']>[0]) => {
      await stabilize(page);
      return screenshot(options);
    };
    await use(page);
  },
  observe: [
    async ({ page }, use, testInfo) => {
      const observations: Observations = { consoleErrors: [], failedRequests: [], loads: [], pages: [] };
      const started = new Map<Request, number>();
      const elapsed = (request: Request): number | null => {
        const start = started.get(request);
        return start === undefined ? null : Date.now() - start;
      };
      page.on('request', (request) => started.set(request, Date.now()));
      page.on('console', (message) => {
        if (message.type() !== 'error') {
          return;
        }
        const location = message.location();
        observations.consoleErrors.push({
          time: new Date().toISOString(),
          pageUrl: page.url(),
          text: message.text(),
          source: location.url ? `${location.url}:${location.lineNumber}` : null,
        });
      });
      page.on('requestfailed', (request) => {
        observations.failedRequests.push({
          time: new Date().toISOString(),
          pageUrl: page.url(),
          method: request.method(),
          url: request.url(),
          status: null,
          failure: request.failure()?.errorText ?? null,
          durationMs: elapsed(request),
        });
      });
      page.on('response', (response) => {
        const request = response.request();
        if (response.status() < FAILED_STATUS || isIgnored(request.method(), response.url(), response.status())) {
          return;
        }
        observations.failedRequests.push({
          time: new Date().toISOString(),
          pageUrl: page.url(),
          method: request.method(),
          url: response.url(),
          status: response.status(),
          failure: null,
          durationMs: elapsed(request),
        });
      });
      page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) {
          observations.pages.push(frame.url());
        }
      });
      page.on('load', async () => {
        try {
          const duration = await page.evaluate(() => {
            const entry = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
            return entry ? entry.duration : null;
          });
          if (duration !== null) {
            observations.loads.push({ pageUrl: page.url(), durationMs: Math.round(duration) });
          }
        } catch {
          // 页面在读取耗时之前已关闭或跳转，这一次加载不记耗时
        }
      });
      await use();
      await testInfo.attach(OBSERVATIONS, { body: JSON.stringify(observations), contentType: 'application/json' });
    },
    { auto: true },
  ],
});

export { expect };
