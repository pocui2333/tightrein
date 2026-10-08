// 每个角色一条 setup 用例：用工作区 e2e/login.ts 导出的 login(page, account, password) 走前端自己的登录流程，
// 把登录态(cookie 与本地存储)保存为 storageState。账号名取自计划文件，密码只经环境变量 TIGHTREIN_PASSWORD_<角色> 传入。
import * as fs from 'fs';
import { test as setup, type Page } from '@playwright/test';

interface PlanProject {
  name: string;
  role: string;
  account?: string;
  storageState: string;
}

type Login = (page: Page, account: string, password: string) => Promise<void>;

setup('登录并保存登录态', async ({ page }, testInfo) => {
  const plan = JSON.parse(fs.readFileSync(process.env.TIGHTREIN_PAGE_PLAN as string, 'utf-8'));
  const project: PlanProject | undefined = plan.projects.find(
    (item: PlanProject) => item.name === testInfo.project.name,
  );
  if (!project || !project.account) {
    throw new Error(`计划文件中没有项目 ${testInfo.project.name} 的账号`);
  }
  const password = process.env[`TIGHTREIN_PASSWORD_${project.role}`];
  if (!password) {
    throw new Error(`没有角色 ${project.role} 的密码`);
  }
  const { login } = (await import(plan.loginModule)) as { login: Login };
  await login(page, project.account, password);
  await page.context().storageState({ path: project.storageState });
});
