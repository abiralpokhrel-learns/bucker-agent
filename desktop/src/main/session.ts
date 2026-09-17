import {AcpClient} from './acp';

export async function startSession(client: AcpClient, cwd: string, previous: string | null, emit: (event: any) => void): Promise<string> {
  // Install listeners before load: history is streamed during that request.
  client.on('update', params => emit({type:'update', sessionId:params.sessionId, update:params.update}));
  client.on('permission', params => emit({type:'permission', ...params}));
  const initialized = await client.request('initialize', {
    protocolVersion:1,
    clientCapabilities:{fs:{readTextFile:false,writeTextFile:false},terminal:false},
    clientInfo:{name:'bucker-desktop',version:'2.0.0'},
  },45000);
  if (previous && initialized.agentCapabilities?.loadSession) {
    await client.request('session/load',{sessionId:previous,cwd,mcpServers:[]},45000);
    return previous;
  }
  const created = await client.request('session/new',{cwd,mcpServers:[]},45000);
  if (typeof created.sessionId !== 'string') throw new Error('The agent did not return a session ID');
  return created.sessionId;
}

export function cancelSession(client: AcpClient, sessionId: string) {
  client.notify('session/cancel',{sessionId});
}
