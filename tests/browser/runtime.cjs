// Resolve the single frontend lockfile's tools without another dependency tree.
const { createRequire } = require('node:module');
const path = require('node:path');
module.exports = createRequire(path.resolve(__dirname, '../../ui/package.json'))('@playwright/test');
