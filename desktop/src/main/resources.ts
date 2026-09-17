import * as path from 'node:path';

export function resourceLayout(packaged: boolean, resources: string, mainDir: string) {
  const root = packaged ? resources : path.resolve(mainDir, '../../..');
  const runtime = packaged ? path.join(root, 'runtime') : path.join(root, 'desktop/build/runtime');
  return {
    runtime,
    python: path.join(runtime, 'python/python.exe'),
    launcher: path.join(runtime, 'launch.py'),
    renderer: packaged ? path.join(root, 'renderer/desktop.html') : path.join(root, 'bucker/frontend/dist/desktop.html'),
  };
}
