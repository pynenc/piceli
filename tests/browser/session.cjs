// Every browser context first opens the launch URL once, as a user does with
// the address `piceli ui serve` prints. The runner generates the token and
// passes it to the server and to Playwright through the environment only.
const base = require('./runtime.cjs');

function launchToken() {
  const token = process.env.PICELI_UI_LAUNCH_TOKEN;
  if (!token) throw new Error('Run through the repository browser runner: PICELI_UI_LAUNCH_TOKEN is missing.');
  return token;
}

async function openLaunchUrl(context, baseURL) {
  // The context's request client shares its cookie jar, so this sets the
  // session without loading (and caching) the application first.
  const url = new URL(`/?token=${encodeURIComponent(launchToken())}`, baseURL).href;
  const response = await context.request.get(url, { maxRedirects: 0 });
  const location = response.headers().location || '';
  if (response.status() !== 303 || location.includes('token=')) {
    throw new Error(`The launch URL was not accepted (${response.status()}).`);
  }
}

const test = base.test.extend({
  context: async ({ context, baseURL }, use) => {
    await openLaunchUrl(context, baseURL);
    await use(context);
  },
});

module.exports = { ...base, test, openLaunchUrl };
