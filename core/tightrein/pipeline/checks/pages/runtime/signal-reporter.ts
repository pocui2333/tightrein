// 自定义 reporter：每条用例在最终一次尝试结束后向 outputFile 追加一行 JSON(tightrein/pipeline/checks/pages/result_parser.py 读取)。
// failedStep 为最后一次尝试中最内层出错的 test.step 标题，没有步骤时为用例标题；error 取前 2000 个字符；
// attachments 汇总各次尝试的截图、trace、视频路径与页面观察。
import * as fs from 'fs';
import * as path from 'path';
import type { Reporter, TestCase, TestResult, TestStep } from '@playwright/test/reporter';

const ERROR_LIMIT = 2000;
const OBSERVATIONS = 'tightrein-observations';
const MEDIA = ['screenshot', 'trace', 'video'];

interface Options {
  outputFile: string;
}

class SignalReporter implements Reporter {
  private readonly outputFile: string;
  private readonly failedSteps = new Map<string, string>();

  constructor(options: Options) {
    this.outputFile = options.outputFile;
    fs.mkdirSync(path.dirname(this.outputFile), { recursive: true });
    fs.writeFileSync(this.outputFile, '');
  }

  onStepEnd(test: TestCase, result: TestResult, step: TestStep): void {
    const key = `${test.id}:${result.retry}`;
    if (step.category === 'test.step' && step.error && !this.failedSteps.has(key)) {
      this.failedSteps.set(key, step.title);
    }
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const final = result.status === 'passed' || result.status === 'skipped' || result.retry >= test.retries;
    if (!final) {
      return;
    }
    const attachments: Record<string, unknown[]> = { screenshot: [], trace: [], video: [], observations: [] };
    for (const attempt of test.results) {
      for (const attachment of attempt.attachments) {
        if (attachment.name === OBSERVATIONS) {
          const body = attachment.body ?? (attachment.path ? fs.readFileSync(attachment.path) : undefined);
          if (body) {
            attachments.observations.push(JSON.parse(body.toString('utf-8')));
          }
        } else if (MEDIA.includes(attachment.name) && attachment.path) {
          attachments[attachment.name].push(attachment.path);
        }
      }
    }
    const project = test.parent.project();
    const failed = result.status !== 'passed' && result.status !== 'skipped';
    const line = {
      title: test.title,
      file: project?.testDir ? path.relative(project.testDir, test.location.file) : test.location.file,
      project: project?.name ?? null,
      role: (project?.metadata?.role as string | undefined) ?? null,
      tags: test.tags,
      outcome: test.outcome(),
      failedStep: failed ? this.failedSteps.get(`${test.id}:${result.retry}`) ?? test.title : null,
      error: result.error?.message ? result.error.message.slice(0, ERROR_LIMIT) : null,
      attachments,
      startedAt: result.startTime.toISOString(),
      durationMs: result.duration,
      retries: result.retry + 1,
    };
    fs.appendFileSync(this.outputFile, JSON.stringify(line) + '\n');
  }
}

export default SignalReporter;
