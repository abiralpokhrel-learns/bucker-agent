const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const syncfs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
test('workspace confines access and refuses to overwrite external changes', async () => {
  assert.ok(syncfs.existsSync(path.resolve(__dirname,'../dist/main/workspace.js')), 'Workspace implementation must exist');
  const {Workspace} = require('../dist/main/workspace.js');
  const dir = await fs.mkdtemp(path.join(os.tmpdir(),'bucker-editor-'));
  try {
    await fs.writeFile(path.join(dir,'a.txt'),'old\r\n');
    const workspace = new Workspace(dir);
    const file = await workspace.read('a.txt');
    await assert.rejects(workspace.read('../escape'), /outside/);
    await assert.rejects(workspace.read('.env'), /protected/);
    await fs.writeFile(path.join(dir,'a.txt'),'changed elsewhere');
    await assert.rejects(workspace.save('a.txt','new',file.version), /changed on disk/);
    const fresh = await workspace.read('a.txt');
    await workspace.save('a.txt','saved\r\n',fresh.version);
    assert.equal(await fs.readFile(path.join(dir,'a.txt'),'utf8'),'saved\r\n');
    assert.equal((await workspace.tree(''))[0].path,'a.txt');
  } finally { await fs.rm(dir,{recursive:true,force:true}); }
});
