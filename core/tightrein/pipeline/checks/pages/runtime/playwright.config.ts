// 从 TIGHTREIN_PAGE_PLAN 指向的计划文件读取全部参数(tightrein/pipeline/checks/pages/plan.py 生成)，本文件不含任何项目取值。
import * as fs from 'fs';
import * as path from 'path';
import { defineConfig, type Project } from '@playwright/test';

interface PlanProject {
  name: string;
  kind: 'setup' | 'patrol' | 'regress';
  role: string;
  storageState?: string;
  testDir?: string;
  testMatch?: string[];
}

interface Plan {
  baseURL: string;
  locale: string;
  retries: number;
  outputDir: string;
  htmlDir: string;
  resultsFile: string;
  projects: PlanProject[];
}

const planPath = process.env.TIGHTREIN_PAGE_PLAN;
if (!planPath) {
  throw new Error('没有设置 TIGHTREIN_PAGE_PLAN，由 tightrein 生成计划文件后再运行');
}
const plan: Plan = JSON.parse(fs.readFileSync(planPath, 'utf-8'));

function project(item: PlanProject): Project {
  if (item.kind === 'setup') {
    return { name: item.name, testDir: __dirname, testMatch: 'auth.setup.ts', metadata: { role: item.role } };
  }
  // 匿名身份没有 storageState，也没有要依赖的 setup 项目
  if (!item.storageState) {
    return { name: item.name, testDir: item.testDir, testMatch: item.testMatch, metadata: { role: item.role } };
  }
  return {
    name: item.name,
    testDir: item.testDir,
    testMatch: item.testMatch,
    dependencies: [`setup-${item.role}`],
    metadata: { role: item.role },
    use: { storageState: item.storageState },
  };
}

export default defineConfig({
  retries: plan.retries,
  outputDir: plan.outputDir,
  reporter: [
    ['html', { outputFolder: plan.htmlDir, open: 'never' }],
    [path.join(__dirname, 'signal-reporter.ts'), { outputFile: plan.resultsFile }],
  ],
  use: {
    baseURL: plan.baseURL,
    locale: plan.locale,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: plan.projects.map(project),
});
