// Environment-based bridge test: loads bridge.html in the real Electron app with preload.
const {_electron:electron} = require('@playwright/test');
const path = require('node:path');
(async()=>{
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({args:['.'],env,timeout:60000});
  try {
    const page = await app.firstWindow();
    await page.goto('file://' + path.resolve(__dirname, 'bridge.html'));
    await page.waitForFunction(() => document.getElementById('result').textContent !== 'BRIDGE_PENDING', null, {timeout: 15000});
    console.log('BRIDGE_RESULT: ' + (await page.locator('#result').innerText()));
  } finally {await app.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
