// End-to-end desktop editor flow against a real Electron window: open folder via
// real IPC, browse tree, open a file in Monaco, edit, save, verify on disk.
// No provider credentials; the Hermes step is exercised separately in acp-live.cjs.
const {_electron:electron} = require('@playwright/test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'bucker-e2e-'));
  await fs.writeFile(path.join(dir, 'hello.py'), 'print("hi")\n');
  const executablePath = process.env.BUCKER_PACKAGED_EXE;
  let app = await electron.launch({...(executablePath ? {executablePath,args:[]} : {args:['.']}), env:{...process.env,BUCKER_DESKTOP_HOME:path.join(dir,'profile')}, timeout: 60000});
  if (executablePath && !await app.evaluate(({app})=>app.isPackaged)) throw new Error('Expected installed application');
  const failures = [];
  try {
    const page = await app.firstWindow();
    page.on('pageerror',e=>{ failures.push('Renderer error: '+e.message); console.error('RENDERER:',e.message); });
    console.log('PHASE: initial window');
    await page.waitForLoadState('domcontentloaded');
    const layout = await page.locator('#root > div').evaluate(el => ({display:getComputedStyle(el).display,direction:getComputedStyle(el).flexDirection}));
    if (layout.display !== 'flex' || layout.direction !== 'column') throw new Error('Desktop layout utilities missing: '+JSON.stringify(layout));
    await page.getByTitle('Providers').click();
    await page.getByRole('button',{name:'Google Gemini',exact:true}).waitFor();
    await page.getByRole('button',{name:'Close provider settings'}).click();
    await app.evaluate(({dialog}, folder) => {
      dialog.showOpenDialog = async () => ({canceled:false, filePaths:[folder]});
    }, dir);
    await page.getByRole('button', {name: /Open folder/}).click({timeout: 20000}).catch(async () => {
      // After a prior open, the button label becomes the folder name — click it anyway.
      await page.locator('header button').first().click({timeout: 20000});
    });
    await page.locator('.file-row').filter({hasText:'hello.py'}).waitFor({timeout:10000});
    await page.locator('.file-row').filter({hasText:'hello.py'}).click();
    await page.locator('.monaco-editor').waitFor({timeout: 15000});
    await page.locator('.monaco-editor').click();
    await page.keyboard.press('Control+End');
    await page.keyboard.insertText('\n# edited by e2e');
    await page.waitForTimeout(300);
    await page.keyboard.press('Control+s');
    await page.waitForTimeout(800);
    const saved = await fs.readFile(path.join(dir, 'hello.py'), 'utf8');
    if (!saved.includes('# edited by e2e')) failures.push('save did not persist: ' + JSON.stringify(saved));
    const status = await page.evaluate(async () => (await window.desktop.invoke('status')).data);
    if (status.workspace !== dir) failures.push('workspace mismatch: ' + status.workspace);
    if (status.connected) failures.push('connected must be false before Connect is clicked');
    const screenshot = path.join(__dirname,'../editor-acceptance.png');
    console.log('PHASE: saved file, capturing initial window');
    await page.screenshot({path:screenshot});
    console.log('PHASE: initial screenshot complete; restarting');
    await app.close();
    app = await electron.launch({...(executablePath ? {executablePath,args:[]} : {args:['.']}), env:{...process.env,BUCKER_DESKTOP_HOME:path.join(dir,'profile')}, timeout:60000});
    const reopened = await app.firstWindow();
    await reopened.locator('.file-row').filter({hasText:'hello.py'}).click();
    await reopened.locator('.monaco-editor').waitFor();
    const readBack = await reopened.evaluate(async()=>await window.desktop.invoke('read',{path:'hello.py'}));
    if (!readBack.ok || readBack.data.content !== saved) throw new Error('Saved content did not survive restart');
    console.log('PHASE: restart persistence verified, capturing reopened window');
    await reopened.screenshot({path:screenshot});
    console.log(JSON.stringify({failures,savedContents:saved,restartPersistence:true,workspace:status.workspace,screenshot}));
  } finally {
    await app.close();
    await fs.rm(dir, {recursive:true, force:true, maxRetries:5, retryDelay:100});
  }
  if (failures.length) { console.error('FAILURES: ' + failures.join(' | ')); process.exitCode = 1; }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
