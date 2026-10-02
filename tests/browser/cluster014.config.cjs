const path = require('node:path');
const { defineConfig } = require('./runtime.cjs');

if (!process.env.PICELI_UI_TEST_OUTPUT) throw new Error('Run through tests/browser/run.py.');
const port = process.env.PICELI_UI_TEST_PORT || '4186';
const origin = `http://127.0.0.1:${port}`;
const python = process.env.PICELI_UI_PYTHON || 'uv run --frozen --extra ui python';
module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'cluster014.spec.cjs',
  outputDir: process.env.PICELI_UI_TEST_OUTPUT,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  reporter: 'line',
  use: {
    baseURL: origin, browserName: 'chromium', trace: 'off', screenshot: 'off', video: 'off',
    launchOptions: process.env.PICELI_BROWSER_EXECUTABLE ? { executablePath: process.env.PICELI_BROWSER_EXECUTABLE } : {},
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'tablet', use: { viewport: { width: 768, height: 1024 } } },
    { name: 'phone', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `${python} tests/browser/serve_showcase.py --port ${port}`,
    cwd: path.resolve(__dirname, '../..'), url: origin, reuseExistingServer: false,
    timeout: 60000, gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
