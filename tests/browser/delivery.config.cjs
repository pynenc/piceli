const path = require('node:path');
const { defineConfig } = require('./runtime.cjs');

if (!process.env.PICELI_UI_TEST_OUTPUT || !process.env.PICELI_DELIVERY_RENDERER) {
  throw new Error('Run through uv run --frozen --extra ui python tests/browser/run_delivery.py.');
}
const port = process.env.PICELI_UI_DELIVERY_PORT || '4178';
const origin = `http://127.0.0.1:${port}`;
module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'delivery.spec.cjs',
  outputDir: process.env.PICELI_UI_TEST_OUTPUT,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 600000,
  expect: { timeout: 15000 },
  reporter: 'line',
  use: {
    baseURL: origin,
    browserName: 'chromium',
    reducedMotion: 'reduce',
    trace: 'off', screenshot: 'off', video: 'off',
    launchOptions: process.env.PICELI_BROWSER_EXECUTABLE ? { executablePath: process.env.PICELI_BROWSER_EXECUTABLE } : {},
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'tablet', use: { viewport: { width: 768, height: 1024 } } },
    { name: 'phone', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `uv run --frozen --extra ui python tests/browser/serve_delivery.py --port ${port}`,
    cwd: path.resolve(__dirname, '../..'),
    url: origin,
    reuseExistingServer: false,
    timeout: 60000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 15000 },
  },
});
