const path = require('node:path');
const { defineConfig } = require('./runtime.cjs');
const harbor = require('./harbor.config.cjs');

const port = process.env.PICELI_UI_CONNECTED_PORT || '4197';
const origin = `http://127.0.0.1:${port}`;
const python = process.env.PICELI_UI_PYTHON || 'uv run --frozen --extra ui python';
module.exports = defineConfig({
  ...harbor,
  testMatch: 'connected.spec.cjs',
  use: { ...harbor.use, baseURL: origin },
  webServer: {
    command: `${python} tests/browser/serve_showcase.py --port ${port}`,
    cwd: path.resolve(__dirname, '../..'),
    url: origin,
    reuseExistingServer: false,
    timeout: 60000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
