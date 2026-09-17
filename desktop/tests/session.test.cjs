const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
test('session bridge unwraps updates, forwards approvals and loads without creating an orphan session',async()=>{
  const {startSession,cancelSession} = require('../dist/main/session.js');
  const client = new EventEmitter(); const calls=[]; const events=[];
  client.request=async(method,payload)=>{calls.push(method);if(method==='initialize')return {agentCapabilities:{loadSession:true}}; if(method==='session/load'){client.emit('update',{sessionId:'old',update:{sessionUpdate:'agent_message_chunk',content:{text:'history'}}});return {}; } return {sessionId:'new'};};
  client.notify=(method)=>calls.push('notify:'+method);
  assert.equal(await startSession(client,'fixture','old',e=>events.push(e)),'old');
  assert.equal(events[0].update.content.text,'history');
  assert.deepEqual(calls,['initialize','session/load']);
  cancelSession(client,'old');assert.equal(calls.at(-1),'notify:session/cancel');
});
