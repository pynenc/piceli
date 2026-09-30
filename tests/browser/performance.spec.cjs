const { test, expect, chromium, openLaunchUrl } = require('./session.cjs');
const { performance } = require('node:perf_hooks');
const os = require('node:os');

const percentile = (values, fraction) => {
  const sorted = [...values].sort((a, b) => a - b);
  return Math.round(sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))]);
};

test('measure ten browsers against the 50-app/5,000-object fixture', async ({}, testInfo) => {
  const browsers = [];
  const pages = [];
  try {
    for (let index = 0; index < 10; index += 1) {
      const browser = await chromium.launch({
        headless: true,
        executablePath: process.env.PICELI_BROWSER_EXECUTABLE || undefined,
      });
      browsers.push(browser);
      const context = await browser.newContext({ viewport: testInfo.project.use.viewport });
      await openLaunchUrl(context, testInfo.project.use.baseURL);
      const page = await context.newPage();
      pages.push(page);
      const cdp = await context.newCDPSession(page);
      await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
      await page.addInitScript(() => {
        window.__piceliLongTasks = [];
        window.__piceliFirstAppTime = null;
        new PerformanceObserver(list => {
          for (const entry of list.getEntries()) window.__piceliLongTasks.push(entry.duration);
        }).observe({ entryTypes: ['longtask'] });
        document.addEventListener('DOMContentLoaded', () => {
          const observer = new MutationObserver(() => {
            if (document.querySelector('a[href="/applications/app-0/overview"]')) {
              window.__piceliFirstAppTime = performance.now();
              observer.disconnect();
            }
          });
          observer.observe(document.body, { childList: true, subtree: true });
        });
      });
    }

    const listTimes = [];
    for (const page of pages) {
      const started = performance.now();
      await page.goto('/applications', { waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('link', { name: 'App 0', exact: true })).toBeVisible({ timeout: 60000 });
      const browserTime = await page.evaluate(() => {
        const api = performance.getEntriesByType('resource')
          .filter(entry => entry.name.endsWith('/api/v1/applications')).at(-1);
        return {
          firstApp: window.__piceliFirstAppTime,
          apiStart: api?.startTime ?? null,
          apiEnd: api?.responseEnd ?? null,
        };
      });
      listTimes.push({ automation: performance.now() - started, ...browserTime });
    }
    const resourceTimes = await Promise.all(pages.map(async (page, index) => {
      const started = performance.now();
      await page.goto(`/applications/app-${index}/resources`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('button', { name: /^Inspect / }).first()).toBeVisible({ timeout: 60000 });
      return performance.now() - started;
    }));
    const selectionTimes = await Promise.all(pages.map(async page => {
      const duration = await page.evaluate(() => new Promise(resolve => {
        const button = document.querySelector('button[aria-label^="Inspect "]');
        if (!button) throw new Error('inspect action unavailable');
        const started = performance.now();
        const observer = new MutationObserver(() => {
          if (document.querySelector('[role="dialog"]')) {
            observer.disconnect();
            requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - started)));
          }
        });
        observer.observe(document.body, { childList: true, subtree: true });
        button.click();
      }));
      await expect(page.getByRole('dialog', { name: 'Resource details' })).toBeVisible();
      return duration;
    }));

    await pages[0].keyboard.press('Escape');
    await expect(pages[0].getByRole('dialog', { name: 'Resource details' })).toHaveCount(0);
    const scrollTasks = await pages[0].evaluate(async () => {
      window.__piceliLongTasks = [];
      const node = document.querySelector('.resource-scroll');
      if (!node) throw new Error('resource scroll region unavailable');
      const distance = node.scrollHeight - node.clientHeight;
      for (let frame = 0; frame < 60; frame += 1) {
        node.scrollTop = distance * frame / 59;
        await new Promise(resolve => requestAnimationFrame(resolve));
      }
      await new Promise(resolve => setTimeout(resolve, 200));
      return window.__piceliLongTasks;
    });
    const logPage = pages[testInfo.project.name === 'phone' ? 1 : 0];
    await logPage.bringToFront();
    await logPage.keyboard.press('Escape');
    await expect(logPage.getByRole('dialog', { name: 'Resource details' })).toHaveCount(0);
    await logPage.evaluate(() => {
      document.querySelector('.resource-scroll').scrollTop = 0;
    });
    await logPage.getByRole('button', { name: /^Inspect Pod object-0 / }).click();
    await logPage.getByRole('button', { name: 'Logs', exact: true }).click();
    const logOutput = logPage.getByRole('region', { name: 'Container log lines' });
    await expect(logOutput).toBeVisible({ timeout: 20000 });
    const latestLine = async () => logPage.locator('.log-line').allTextContents().then(lines =>
      Math.max(0, ...lines.map(line => Number(line.match(/burst-(\d+)/)?.[1] ?? 0))));
    await expect.poll(latestLine, { timeout: 20000, intervals: [500] }).toBeGreaterThan(0);
    await logPage.evaluate(() => { window.__piceliLongTasks = []; });
    await expect.poll(latestLine, { timeout: 25000, intervals: [500] }).toBeGreaterThanOrEqual(9000);
    const logBurst = await logPage.evaluate(async () => {
      const node = document.querySelector('.log-output');
      if (!node) throw new Error('log scroll region unavailable');
      const distance = node.scrollHeight - node.clientHeight;
      for (let frame = 0; frame < 60; frame += 1) {
        node.scrollTop = distance * frame / 59;
        await new Promise(resolve => requestAnimationFrame(resolve));
      }
      await new Promise(resolve => setTimeout(resolve, 200));
      return {
        longTasks: window.__piceliLongTasks,
        gap: document.body.textContent.includes('Log history has a gap'),
        scrollHeight: node.scrollHeight,
      };
    });
    const report = {
      fixture: '50 applications, 5000 objects, 3 scopes, 10 Chromium processes',
      list_load_mode: 'sequential cold navigation with all ten browsers alive',
      viewport: testInfo.project.name,
      host: `${os.platform()} ${os.release()} ${os.arch()}`,
      cpu: os.cpus()[0]?.model ?? 'unknown',
      logical_cpus: os.cpus().length,
      cpu_throttle: 4,
      added_api_latency_ms: 100,
      list_automation_p95_ms: percentile(listTimes.map(item => item.automation), 0.95),
      list_browser_p50_ms: percentile(listTimes.map(item => item.firstApp), 0.5),
      list_browser_p95_ms: percentile(listTimes.map(item => item.firstApp), 0.95),
      list_api_start_p95_ms: percentile(listTimes.map(item => item.apiStart), 0.95),
      list_api_end_p95_ms: percentile(listTimes.map(item => item.apiEnd), 0.95),
      resources_p95_ms: percentile(resourceTimes, 0.95),
      selection_visual_p95_ms: percentile(selectionTimes, 0.95),
      scroll_long_task_count: scrollTasks.length,
      scroll_long_task_max_ms: Math.round(Math.max(0, ...scrollTasks)),
      log_latest_index: await latestLine(),
      log_gap_visible: logBurst.gap,
      log_scroll_height_px: logBurst.scrollHeight,
      log_long_task_count: logBurst.longTasks.length,
      log_long_task_max_ms: Math.round(Math.max(0, ...logBurst.longTasks)),
    };
    console.log(`PICELI_UI_BROWSER_PERFORMANCE ${JSON.stringify(report)}`);
    expect(report.list_browser_p95_ms, 'first useful application list budget').toBeLessThan(2000);
    expect(report.selection_visual_p95_ms, 'selection response budget').toBeLessThan(100);
    expect(report.scroll_long_task_max_ms, 'scroll long-task budget').toBeLessThanOrEqual(50);
    expect(report.log_latest_index, 'ten-second log burst continuity').toBeGreaterThanOrEqual(9000);
    expect(report.log_long_task_max_ms, 'log burst/scroll long-task budget').toBeLessThanOrEqual(50);
  } finally {
    await Promise.all(browsers.map(browser => browser.close()));
  }
});
