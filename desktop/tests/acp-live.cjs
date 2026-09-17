// Real installed Hermes protocol smoke; no model request or credentials.
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const {AcpClient} = require('../dist/main/acp.js');
(async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(),'bucker-acp-smoke-'));
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/(API_KEY|TOKEN|SECRET|PASSWORD|HERMES_HOME|HERMES_PROFILE)/i.test(key)));
  const child = spawn(process.env.BUCKER_HERMES_EXECUTABLE || 'hermes', ['acp'], {env:{...env,HERMES_HOME:home,PYTHON_DOTENV_DISABLED:'1'}, cwd:home, windowsHide:true, stdio:'pipe'});
  const client = new AcpClient(child);
  try {
    const result = await client.request('initialize',{protocolVersion:1,clientCapabilities:{fs:{readTextFile:false,writeTextFile:false},terminal:false},clientInfo:{name:'bucker-desktop-smoke',version:'0.1.0'}},60000);
    assert.equal(result.agentInfo.name,'hermes-agent');
    assert.equal(result.agentCapabilities.loadSession,true);
    console.log(JSON.stringify({actualAgent:result.agentInfo,protocolVersion:result.protocolVersion,loadSession:result.agentCapabilities.loadSession,modelCalls:0}));
  } finally {
    const exited = new Promise(resolve => child.once('exit',resolve));
    client.close(); await exited;
    await fs.rm(home,{recursive:true,force:true,maxRetries:5,retryDelay:100});
  }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
