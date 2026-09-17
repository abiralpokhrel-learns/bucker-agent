// Real bundled Hermes handshake + gateway; no provider inference.
// Order mirrors the real app: gateway starts first, config.yaml points the
// bundled ACP adapter at the gateway before initialize (otherwise Hermes's
// provider scan lazy-installs optional provider deps like boto3 in the
// background and the handshake stalls).
const {spawn}=require('node:child_process');
const fs=require('node:fs/promises'); const path=require('node:path'); const os=require('node:os');
const assert=require('node:assert/strict');
const {AcpClient}=require('../dist/main/acp');
const {sidecarEnv,availablePort,waitReady}=require('../dist/main/runtime');
(async()=>{
 const runtime=path.resolve(process.env.BUCKER_RUNTIME || 'build/runtime');
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'bucker-bundle-'));
 const env={...sidecarEnv(process.env,home),PATH:process.env.SystemRoot+'\\System32'};
 const python=path.join(runtime,'python/python.exe'); const launcher=path.join(runtime,'launch.py');
 const token='local-test-token-not-a-provider-key';
 let agent, gateway;
 try {
  // 1. Gateway first, exactly like desktop/src/main/index.ts startGateway().
  const port=await availablePort();
  gateway=spawn(python,[launcher,'gateway','--port',String(port)],{cwd:home,env:{...env,BUCKER_DESKTOP_TOKEN:token,BUCKER_DESKTOP_CONNECTIONS:JSON.stringify([{provider:'groq',base_url:'https://api.groq.com/openai/v1',api_key:'invalid-test-placeholder',model:'test-model',context:32000,max_output:4096,free:true,tools:true}])},stdio:'pipe',windowsHide:true});
  gateway.stderr.on('data',x=>process.stderr.write(x));
  await waitReady(gateway,`http://127.0.0.1:${port}/health/live`,token,30000);
  assert.equal((await fetch(`http://127.0.0.1:${port}/v1/models`)).status,401);
  const models=await fetch(`http://127.0.0.1:${port}/v1/models`,{headers:{Authorization:`Bearer ${token}`}});
  assert.equal(models.status,200);
  // 2. Bundled ACP with config.yaml pointing at the gateway (writeHermesConfig).
  await fs.writeFile(path.join(home,'config.yaml'), JSON.stringify({model:{provider:'custom',default:'desktop-auto',api_mode:'chat_completions',base_url:`http://127.0.0.1:${port}/v1`,api_key:token}}),'utf8');
  agent=spawn(python,[launcher,'acp'],{cwd:home,env,stdio:'pipe',windowsHide:true});
  agent.stderr.on('data',x=>process.stderr.write(x));
  const client=new AcpClient(agent);
  const init=await client.request('initialize',{protocolVersion:1,clientCapabilities:{fs:{readTextFile:false,writeTextFile:false},terminal:false},clientInfo:{name:'bundle-test',version:'1'}},60000);
  assert.equal(init.agentInfo.name,'hermes-agent');
  console.log(JSON.stringify({bundledAgent:init.agentInfo,gatewayModels:await models.json(),noHostPython:true,modelCalls:0}));
 } finally {try{agent?.kill();gateway?.kill();}catch{}await new Promise(r=>setTimeout(r,500));await fs.rm(home,{recursive:true,force:true,maxRetries:5,retryDelay:200}).catch(()=>{});}
})().catch(e=>{console.error('TEST-ERROR:',e.message);process.exitCode=1;});
