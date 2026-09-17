const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
test('runtime bundle has standalone Python, launchers, licenses and no user config',()=>{
 const root=path.resolve(__dirname,'../build/runtime');
 for(const file of ['python/python.exe','launch.py','inventory.json']) assert.ok(fs.existsSync(path.join(root,file)),file);
 assert.ok(!fs.existsSync(path.join(root,'.env')));
});
 test('packaged resource paths never reference the source checkout',()=>{
 const {resourceLayout}=require('../dist/main/resources');
 const r=resourceLayout(true,'C:/Installed/resources','C:/source/desktop/dist/main');
 assert.ok(r.python.includes('Installed')); assert.ok(r.renderer.includes('Installed'));
 assert.ok(Object.values(r).every(x=>x.startsWith(path.normalize('C:/Installed/resources'))));
 assert.ok(!Object.values(r).some(x=>x.startsWith(path.normalize('C:/source'))));
});
 test('installer includes runtime and renderer with explicit file lists',()=>{
 const p=require('../package.json');
 assert.ok(p.build.extraResources.some(x=>x.to==='runtime'));
 assert.ok(p.build.extraResources.some(x=>x.to==='renderer'));
 assert.equal(p.build.win.target[0],'nsis');
});
 