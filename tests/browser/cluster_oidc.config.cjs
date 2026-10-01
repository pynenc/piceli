const { defineConfig } = require('./runtime.cjs');

if (!process.env.PICELI_UI_TEST_OUTPUT || !process.env.PICELI_OIDC_TEST_URL) {
  throw new Error('Run through python tests/browser/run_cluster_oidc.py for self-cleaning artifacts.');
}

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'cluster_oidc.spec.cjs',
  outputDir: process.env.PICELI_UI_TEST_OUTPUT,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  reporter: 'line',
  use: {
    baseURL: process.env.PICELI_OIDC_TEST_URL,
    browserName: 'chromium',
    ignoreHTTPSErrors: true, // Only for the throwaway localhost certificate.
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    launchOptions: process.env.PICELI_BROWSER_EXECUTABLE ? { executablePath: process.env.PICELI_BROWSER_EXECUTABLE } : {},
  },
});
