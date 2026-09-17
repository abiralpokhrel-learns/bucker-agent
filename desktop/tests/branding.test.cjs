// White-label regression: user-visible agent failure strings must not name
// the internal engine, while internal identifiers (hermesPath, HERMES_HOME)
// must stay intact for the bundled runtime.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const USER_VISIBLE_PATTERNS = [
  /Hermes exited/,
  /Hermes connection is closed/,
  /Hermes input closed/,
  /Unable to start Hermes/,
  /Hermes request failed/,
  /Hermes session setup failed/,
  /Hermes did not return/,
  /reconnect Hermes/,
  /Choose Hermes/,
  /Hermes is not installed/,
  /Bundled Hermes runtime/,
];

test('main-process user-facing strings are white-labeled to Bucker', () => {
  const dir = path.resolve(__dirname, '../dist/main');
  for (const file of ['acp.js', 'session.js', 'index.js']) {
    const source = fs.readFileSync(path.join(dir, file), 'utf8');
    for (const pattern of USER_VISIBLE_PATTERNS) {
      assert.doesNotMatch(source, pattern, `${file} still shows "${pattern}" to the user`);
    }
  }
});

test('internal runtime identifiers are preserved', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../dist/main/index.js'), 'utf8');
  assert.match(source, /hermesPath/);
  assert.match(source, /HERMES_HOME|hermes-home/);
});
