const path = require('node:path');
const { defineConfig } = require('./runtime.cjs');

if (!process.env.PICELI_UI_TEST_OUTPUT) {
  throw new Error('Run through uv run --frozen python tests/browser/run_clips.py for self-cleaning artifacts.');
}
const port = process.env.PICELI_UI_TEST_PORT || '4185';
const origin = `http://127.0.0.1:${port}`;
const python = process.env.PICELI_UI_PYTHON || 'uv run --frozen --extra ui python';
module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'clips.spec.cjs',
  outputDir: process.env.PICELI_UI_TEST_OUTPUT,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  reporter: 'line',
  use: {
    baseURL: origin,
    browserName: 'chromium',
    viewport: { width: 1100, height: 680 },
    video: { mode: 'on', size: { width: 1100, height: 680 } },
    launchOptions: process.env.PICELI_BROWSER_EXECUTABLE ? { executablePath: process.env.PICELI_BROWSER_EXECUTABLE } : {},
  },
  webServer: {
    command: `${python} tests/browser/serve_showcase.py --port ${port}`,
    cwd: path.resolve(__dirname, '../..'),
    url: origin,
    reuseExistingServer: false,
    timeout: 60000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
