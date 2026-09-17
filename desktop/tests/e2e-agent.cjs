// Local stdio contract test, not an Electron UI test or live inference test.
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const assert = require('node:assert/strict');
const path = require('node:path');
const {AcpClient} = require('../dist/main/acp.js');
(async () => {
  const child = spawn(process.execPath,[path.join(__dirname,'fixtures/acp-agent.cjs')],{stdio:'pipe'});
  const client = new AcpClient(child);
  const updates = [];
  client.on('update', value => updates.push(value));
  client.on('permission', value => client.respondPermission(value.id,'allow'));
  try {
    assert.equal((await client.request('initialize',{protocolVersion:1})).protocolVersion,1);
    const session = await client.request('session/new',{});
    assert.equal(session.sessionId,'fixture-session');
    const result = await client.request('session/prompt',{sessionId:session.sessionId,prompt:[{type:'text',text:'fixture'}]},5000);
    assert.equal(result.stopReason,'end_turn');
    assert.equal(updates[0].update.sessionUpdate,'plan');
    assert.equal(updates[1].update.content.text,'Fixture text');
    client.notify('session/cancel',{sessionId:session.sessionId});
    console.log(JSON.stringify({peer:'scripted local ACP fixture',updates:updates.length,permission:'answered',stopReason:result.stopReason,modelCalls:0}));
  } finally { const exited = once(child,'exit'); client.close(); await exited; }
})().catch(error=>{console.error(error);process.exitCode=1;});
