// Minimal deterministic probe: does the window mount DesktopApp with an Open folder button?
// Read-only: dumps button inventory + console errors. Edits nothing.
const {_electron:electron} = require('@playwright/test');
const path = require('node:path');
(async()=>{
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({args:[path.resolve(__dirname,'..')],env,timeout:60000});
  try {
    const page = await app.firstWindow();
    const consoleLogs = [];
    page.on('console', m => consoleLogs.push(`${m.type()}: ${m.text().slice(0,200)}`));
    const pageErrors = [];
    page.on('pageerror', e => pageErrors.push(e.message.slice(0,300)));
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(2500);
    const buttons = await page.locator('button').allInnerTexts();
    console.log(JSON.stringify({
      url: page.url(),
      title: await page.title(),
      bodyHead: (await page.locator('body').innerText()).slice(0,300),
      buttonCount: buttons.length,
      buttons: buttons.slice(0,8),
      pageErrors, consoleLogs: consoleLogs.slice(0,10),
    }, null, 1));
    await page.screenshot({path:path.resolve(__dirname,'../probe.png')});
  } finally {await app.close();}
})().catch(e=>{console.error('PROBE_FAIL '+e.message);process.exitCode=1;});
