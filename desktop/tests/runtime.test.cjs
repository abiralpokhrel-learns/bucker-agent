const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
test('sidecar environment excludes inherited credentials and isolates Hermes home', () => {
  assert.ok(fs.existsSync(path.resolve(__dirname,'../dist/main/runtime.js')), 'Runtime implementation must exist');
  const {sidecarEnv} = require('../dist/main/runtime.js');
  const env = sidecarEnv({PATH:'tools',HOME:'home',OPENROUTER_API_KEY:'private',HERMES_HOME:'personal',AWS_ACCESS_KEY_ID:'private',NODE_OPTIONS:'inject',PYTHONPATH:'inject'},'app-home');
  assert.equal(env.PATH,'tools'); assert.equal(env.HERMES_HOME,'app-home');
  for (const k of ['OPENROUTER_API_KEY','AWS_ACCESS_KEY_ID','NODE_OPTIONS','PYTHONPATH']) assert.equal(env[k],undefined);
  assert.equal(env.PYTHON_DOTENV_DISABLED,'1');
});
