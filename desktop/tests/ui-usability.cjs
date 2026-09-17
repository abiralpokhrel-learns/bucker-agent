// Real Electron UI acceptance; no credentials or inference.
const { _electron: electron } = require('@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
 const home = await fs.mkdtemp(path.join(os.tmpdir(), 'bucker-ui-'));
 const exe = process.env.BUCKER_PACKAGED_EXE;
 const app = await electron.launch({...(exe ? {executablePath:exe,args:[]} : {args:['.']}), env:{...process.env,BUCKER_DESKTOP_HOME:home},timeout:60000});
 const errors=[];
 try {
  const page=await app.firstWindow();
  page.on('pageerror',e=>errors.push(e.message));
  await page.getByRole('heading',{name:'A little context. A lot of possibility.'}).waitFor({timeout:10000});
  await page.getByRole('button',{name:'Set up AI'}).click();
  await page.getByRole('heading',{name:'Connect your AI'}).waitFor();
  await page.getByRole('button',{name:'Google Gemini',exact:true}).click();
  assert.equal(await page.locator('input[type=password]').count(),1);
  assert.equal(await page.getByRole('button',{name:'Save connection',exact:true}).isDisabled(),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('heading',{name:'Connect your AI'}).count(),0);
  await page.getByRole('button',{name:'Toggle Explorer',exact:true}).click();
  assert.equal(await page.locator('[aria-label="Project files"]').count(),0);
  await page.keyboard.press('Control+b');
  await page.locator('[aria-label="Project files"]').waitFor();
  await page.getByRole('button',{name:'Toggle activity',exact:true}).click();
  await page.getByText('Your tool output, in one place.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Toggle activity',exact:true}).click();
  for(const [w,h] of [[1440,920],[1000,720]]) {
   await app.evaluate(({BrowserWindow},size)=>BrowserWindow.getAllWindows()[0].setSize(...size),[w,h]);
   await page.waitForTimeout(250);
   const geometry=await page.evaluate(()=>({width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,panes:[...document.querySelectorAll('.workspace-pane,.assistant-pane')].map(e=>({width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right}))}));
   assert.equal(geometry.overflow,false,JSON.stringify(geometry));
   assert.ok(geometry.panes.every(p=>p.width>=250 && p.right<=geometry.width+1),JSON.stringify(geometry));
   await page.screenshot({path:path.resolve(`ui-welcome-${w}.png`)});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({welcome:true,focusedProviderSetup:true,keyboardShortcuts:true,panelToggles:true,windowSizes:[1440,1000],rendererErrors:errors}));
 } finally {await app.close();await fs.rm(home,{recursive:true,force:true,maxRetries:5,retryDelay:200});}
})().catch(e=>{console.error(e);process.exitCode=1;});
