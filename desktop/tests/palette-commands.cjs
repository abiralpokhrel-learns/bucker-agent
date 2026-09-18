// Palette micro-features: close-tab + stop-agent commands, real Electron.
// No inference or credentials; asserts the new palette commands behave.
const {_electron: electron, expect} = require('@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'bucker-palette-'));
  await fs.writeFile(path.join(dir, 'hello.py'), 'print("hi")\n');
  const app = await electron.launch({args:[path.resolve(__dirname, '..')], env:{...process.env, BUCKER_DESKTOP_HOME:path.join(dir,'profile')}, timeout:60000});
  const errors = [];
  try {
    const page = await app.firstWindow();
    page.on('pageerror', e => errors.push(e.message));
    await page.locator('.bucker-shell').waitFor();
    await app.evaluate(({dialog},folder)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[folder]});},dir);
    await page.locator('.workspace-switch').click();
    await page.locator('.file-row').filter({hasText:'hello.py'}).click();
    await expect(page.locator('.monaco-editor')).toBeVisible();
    await expect(page.locator('.editor-tab')).toHaveCount(1);

    // Close the active tab from the palette.
    await page.keyboard.press('Control+Shift+p');
    const palette = page.getByRole('dialog',{name:'Command palette'});
    await expect(palette).toBeVisible();
    await page.getByRole('textbox',{name:'Search commands'}).fill('close active tab');
    await expect(palette.getByRole('option',{name:/Close active tab/})).toBeVisible();
    await page.keyboard.press('Enter');
    await expect(palette).toHaveCount(0);
    await expect(page.locator('.editor-tab')).toHaveCount(0);

    // With no tab open the command is offered but disabled, never destructive.
    await page.keyboard.press('Control+Shift+p');
    await page.getByRole('textbox',{name:'Search commands'}).fill('close active tab');
    await expect(palette.getByRole('option',{name:/Close active tab/})).toHaveAttribute('aria-disabled','true');
    await page.keyboard.press('Escape');
    await expect(palette).toHaveCount(0);

    // Stop agent is disabled while idle (nothing running to stop).
    await page.keyboard.press('Control+Shift+p');
    await page.getByRole('textbox',{name:'Search commands'}).fill('stop agent');
    await expect(palette.getByRole('option',{name:/Stop agent/})).toHaveAttribute('aria-disabled','true');
    await page.keyboard.press('Escape');
    await expect(palette).toHaveCount(0);

    assert.deepEqual(errors, []);
    console.log(JSON.stringify({paletteCommands:true,rendererErrors:errors}));
  } finally {
    await app.close();
    await fs.rm(dir,{recursive:true,force:true,maxRetries:5,retryDelay:200});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
