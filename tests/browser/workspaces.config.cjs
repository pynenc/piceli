const path = require('node:path');
const { defineConfig } = require('./runtime.cjs');
const harbor = require('./harbor.config.cjs');

// Logs, Forwards, the compact Overview and the navigation, on the showcase.
const port = process.env.PICELI_UI_WORKSPACES_PORT || '4203';
const origin = `http://127.0.0.1:${port}`;
const python = process.env.PICELI_UI_PYTHON || 'uv run --frozen --extra ui python';
module.exports = defineConfig({
  ...harbor,
  testMatch: 'workspaces.spec.cjs',
  timeout: 45000,
  use: { ...harbor.use, baseURL: origin },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'wide', use: { viewport: { width: 1920, height: 1080 } } },
    { name: 'tablet', use: { viewport: { width: 768, height: 1024 } } },
    { name: 'phone', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `${python} tests/browser/serve_showcase.py --port ${port}`,
    cwd: path.resolve(__dirname, '../..'),
    url: origin,
    reuseExistingServer: false,
    timeout: 60000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
