const {_electron:electron} = require('@playwright/test');
const path = require('node:path');
(async()=>{
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({args:[path.resolve(__dirname,'..')],env,timeout:60000});
  try {
    const page = await app.firstWindow();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);
    console.log(JSON.stringify({url:page.url(),title:await page.title(),text:(await page.locator('body').innerText()).slice(0,1500),errors}));
    await page.screenshot({path:path.resolve(__dirname,'../window-smoke.png')});
  } finally {await app.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
