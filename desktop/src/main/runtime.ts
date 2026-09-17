import {spawn, ChildProcessWithoutNullStreams} from 'node:child_process';
import {createServer} from 'node:net';
/** Allowlist prevents inheriting the developer's personal provider credentials. */
export function sidecarEnv(source: NodeJS.ProcessEnv, home: string): NodeJS.ProcessEnv {
  const allowed = /^(PATH|PATHEXT|SYSTEMROOT|WINDIR|COMSPEC|TEMP|TMP|HOME|USERPROFILE|LOCALAPPDATA|APPDATA|PROGRAMFILES|PROGRAMFILES\(X86\)|LANG|LC_ALL|NUMBER_OF_PROCESSORS)$/i;
  return {...Object.fromEntries(Object.entries(source).filter(([key])=>allowed.test(key))), HERMES_HOME:home, PYTHON_DOTENV_DISABLED:'1', PYTHONUTF8:'1', PYTHONIOENCODING:'utf-8'};
}
export async function availablePort(): Promise<number> {
  const server = createServer();
  return new Promise((resolve,reject)=>{
    server.once('error',reject); server.listen(0,'127.0.0.1',()=>{
      const port = (server.address() as {port:number}).port;
      server.close(error=> error ? reject(error) : resolve(port));
    });
  });
}
export function runProcess(exe: string, args: string[], cwd: string, env: NodeJS.ProcessEnv, timeoutMs=30000): Promise<void> {
  return new Promise((resolve,reject)=>{
    const child = spawn(exe,args,{cwd,env,stdio:'ignore',windowsHide:true});
    const timer = setTimeout(()=>{child.kill();reject(new Error('Runtime setup timed out'));},timeoutMs);
    child.once('error',()=>{clearTimeout(timer);reject(new Error('Runtime executable unavailable'));});
    child.once('exit',code=>{clearTimeout(timer);code===0 ? resolve() : reject(new Error(`Runtime setup failed (${code})`));});
  });
}
export async function waitReady(child: ChildProcessWithoutNullStreams, url: string, token: string, timeoutMs=30000) {
  const deadline = Date.now()+timeoutMs;
  while(Date.now()<deadline) {
    if(child.exitCode !== null) throw new Error('Gateway process exited before readiness');
    try { const response = await fetch(url,{headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(1000)}); if(response.ok) return; } catch {}
    await new Promise(resolve=>setTimeout(resolve,200));
  }
  throw new Error('Gateway readiness timed out');
}
