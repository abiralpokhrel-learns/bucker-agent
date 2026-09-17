// Refresh first-party sources before packaging an existing bundled runtime.
// The Python/runtime dependency bundle is built separately by build_runtime.py.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const runtime = path.join(root, 'desktop/build/runtime');
for (const file of ['python/python.exe', 'hermes/pyproject.toml', 'launch.py', 'inventory.json']) {
  if (!fs.existsSync(path.join(runtime, file))) {
    throw new Error(`Missing runtime/${file}; build the runtime before packaging.`);
  }
}
const target = path.join(runtime, 'gateway/bucker');
fs.mkdirSync(target, {recursive: true});
for (const name of ['gateway', 'desktop_gateway']) {
  fs.rmSync(path.join(target, name), {recursive: true, force: true});
  fs.cpSync(path.join(root, 'bucker', name), path.join(target, name), {
    recursive: true,
    filter: source => !['__pycache__', '.env', 'auth.json', 'config.yaml'].includes(path.basename(source))
      && !path.basename(source).startsWith('.env.') && !source.endsWith('.pyc'),
  });
}
for (const name of ['__init__.py', 'config.py']) {
  fs.copyFileSync(path.join(root, 'bucker', name), path.join(target, name));
}
fs.copyFileSync(path.join(__dirname, 'launch.py'), path.join(runtime, 'launch.py'));
fs.copyFileSync(path.join(root, 'LICENSE'), path.join(runtime, 'BUCKER-LICENSE'));
console.log('Bundled gateway refreshed from current first-party sources.');
