// Scripted ACP peer. NOT Hermes and NOT an inference provider.
const readline = require('node:readline');
const send = message => process.stdout.write(JSON.stringify({jsonrpc:'2.0', ...message}) + '\n');
let pendingPrompt;
const update = value => send({method:'session/update', params:{sessionId:'fixture-session', update:value}});
readline.createInterface({input:process.stdin}).on('line', line => {
  const m = JSON.parse(line);
  if (m.method === 'initialize') send({id:m.id,result:{protocolVersion:1}});
  else if (m.method === 'session/new') send({id:m.id,result:{sessionId:'fixture-session'}});
  else if (m.method === 'session/prompt') {
    pendingPrompt = m.id;
    update({sessionUpdate:'plan',entries:[{content:'Fixture action',priority:'medium',status:'in_progress'}]});
    update({sessionUpdate:'agent_message_chunk',content:{type:'text',text:'Fixture text'}});
    send({id:777,method:'session/request_permission',params:{sessionId:'fixture-session',toolCall:{toolCallId:'fixture-call',title:'Fixture action',status:'pending'},options:[{optionId:'allow',name:'Allow once',kind:'allow_once'}]}});
  } else if (m.id === 777) {
    if (m.result?.outcome?.optionId !== 'allow') process.exit(2);
    send({id:pendingPrompt,result:{stopReason:'end_turn'}});
  } else if (m.method === 'session/cancel') {
    // ACP cancel is a notification, not a request.
    if (m.id !== undefined) process.exit(3);
  }
});
process.stdin.on('end',()=>process.exit(0));
