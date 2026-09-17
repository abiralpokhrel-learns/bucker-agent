import {app, BrowserWindow, ipcMain, dialog, shell, safeStorage} from 'electron';
import {spawn} from 'node:child_process';
import * as path from 'node:path';
import * as fs from 'node:fs';
import * as fsPromises from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {AcpClient} from './acp';
import {resourceLayout} from './resources';
const bundled = () => resourceLayout(app.isPackaged, process.resourcesPath, __dirname);
import {startSession, cancelSession} from './session';
import {Workspace} from './workspace';
import {sidecarEnv, availablePort, waitReady} from './runtime';

const MAX_EVENTS = 400;

interface Connection {provider: string; model: string; apiKey: string;}
interface Persisted {workspace: string | null; hermesPath: string | null; sessionByWorkspace: Record<string, string>; connections: Connection[]; freeConfirmed: string[];}
const emptyState = (): Persisted => ({workspace: null, hermesPath: null, sessionByWorkspace: {}, connections: [], freeConfirmed: []});

/** Official endpoints only; keys are stored OS-encrypted, never sent to the renderer. */
const PROVIDERS: Record<string, {name: string; baseUrl: string; signup: string; docs: string; tier: string; notes: string; models: {id: string; context: number; maxOutput: number}[]; steps: string[]}> = {
  openrouter: {name:'OpenRouter', baseUrl:'https://openrouter.ai/api/v1', signup:'https://openrouter.ai/settings/keys', docs:'https://openrouter.ai/docs', tier:'free', notes:'Only :free models are allowed here. Rate limits are shared and per-model; paid models are never routed.', models:[{id:'nvidia/nemotron-3-ultra-550b-a55b:free', context:1000000, maxOutput:65536}], steps:['Create a free account at openrouter.ai','Open Settings → Keys → Create key','Paste the key below (starts with sk-or-)']},
  gemini: {name:'Google Gemini', baseUrl:'https://generativelanguage.googleapis.com/v1beta/openai', signup:'https://aistudio.google.com/apikey', docs:'https://ai.google.dev/gemini-api/docs', tier:'free', notes:'Free tier has per-minute and per-day limits that vary by model and region.', steps:['Sign in at aistudio.google.com','Open "Get API key" and create a key in your project','Paste the key below'], models:[{id:'gemini-2.5-flash', context:1048576, maxOutput:65536}]},
  groq: {name:'Groq', baseUrl:'https://api.groq.com/openai/v1', signup:'https://console.groq.com/keys', docs:'https://console.groq.com/docs/openai', tier:'free', notes:'Free plan: low requests-per-minute and small tokens-per-minute caps. Good for short bursts, tight for large files.', steps:['Create a Groq account','Open API Keys and create a key','Paste the key below'], models:[{id:'moonshotai/kimi-k2-instruct-0905', context:262144, maxOutput:16384}]},
  mistral: {name:'Mistral', baseUrl:'https://api.mistral.ai/v1', signup:'https://console.mistral.ai/api-keys', docs:'https://docs.mistral.ai/api/', tier:'free', notes:'Free mode: API access with no credit card; pricing advertises monthly free credits. Capacity is shared.', steps:['Create a Mistral console account (phone verification may be required)','Create an API key','Paste the key below'], models:[{id:'mistral-small-latest', context:131072, maxOutput:8192}]},
  sambanova: {name:'SambaNova', baseUrl:'https://api.sambanova.ai/v1', signup:'https://cloud.sambanova.ai', docs:'https://docs.sambanova.ai', tier:'free', notes:'Free tier without a payment method: about 20 requests/day per model. A few agent runs per day.', steps:['Create a SambaNova account','Generate an API key','Paste the key below'], models:[{id:'gpt-oss-120b', context:131072, maxOutput:8192}]},
  huggingface: {name:'Hugging Face', baseUrl:'https://router.huggingface.co/v1', signup:'https://huggingface.co/settings/tokens', docs:'https://huggingface.co/docs/inference-providers', tier:'free', notes:'Free accounts get about $0.10/month of routed inference. Enough to verify your setup works, not for real sessions.', steps:['Create a Hugging Face account','Create a fine-grained token with Inference permission','Paste the token below (starts with hf_)'], models:[{id:'deepseek-ai/DeepSeek-V3-0324', context:131072, maxOutput:8192}]},
};

if (process.env.BUCKER_DESKTOP_HOME) app.setPath('userData', path.resolve(process.env.BUCKER_DESKTOP_HOME));
const state: Persisted = emptyState();
let win: BrowserWindow | null = null;
let workspace: Workspace | null = null;
let acp: AcpClient | null = null;
let sessionId: string | null = null;
let busy = false;
let gateway: {child: any; port: number; token: string} | null = null;
const events: any[] = [];
const appHome = () => app.getPath('userData');
const stateFile = () => path.join(appHome(), 'desktop-state.json');

function persist() {
  try {
    const payload = {workspace: state.workspace, hermesPath: state.hermesPath, sessionByWorkspace: state.sessionByWorkspace, freeConfirmed: state.freeConfirmed, connections: safeStorage.isEncryptionAvailable() ? safeStorage.encryptString(JSON.stringify(state.connections)).toString('base64') : []};
    fs.writeFileSync(stateFile(), JSON.stringify(payload), {mode: 0o600});
  } catch {}
}
function restore() {
  try {
    const raw = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    state.workspace = raw.workspace ?? null;
    state.hermesPath = raw.hermesPath ?? null;
    state.sessionByWorkspace = raw.sessionByWorkspace ?? {};
    state.freeConfirmed = raw.freeConfirmed ?? [];
    const blob = typeof raw.connections === 'string' ? Buffer.from(raw.connections, 'base64') : null;
    state.connections = blob && safeStorage.isEncryptionAvailable() ? JSON.parse(safeStorage.decryptString(blob).toString()) : [];
  } catch {}
}
function pushEvent(event: any) {
  events.push({sessionId, ...event, at: Date.now()});
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
  win?.webContents.send('desktop:event', {sessionId, ...event});
}
const publicStatus = () => ({
  workspace: state.workspace, sessionId, busy, connected: acp !== null && sessionId !== null,
  hermesPath: state.hermesPath,
  connections: state.connections.map(c => ({provider: c.provider, model: c.model})),
  events,
});

async function startGateway() {
  if (gateway || state.connections.length === 0) return;
  const port = await availablePort();
  const token = randomBytes(24).toString('hex');
  const connections = state.connections.map(c => {
    const provider = PROVIDERS[c.provider];
    const model = provider?.models.find(m => m.id === c.model);
    return provider && model ? {provider: c.provider, base_url: provider.baseUrl, api_key: c.apiKey, model: c.model, context: model.context, max_output: model.maxOutput, free: true, tools: true} : null;
  }).filter(Boolean);
  if (connections.length === 0) return;
  const child = spawn(pythonExecutable(), fs.existsSync(bundled().launcher) ? [bundled().launcher,'gateway','--port',String(port)] : ['-m','bucker.desktop_gateway','--port',String(port)], {
    cwd: appRoot(), env: {...sidecarEnv(process.env, appHome()), BUCKER_DESKTOP_TOKEN: token, BUCKER_DESKTOP_CONNECTIONS: JSON.stringify(connections)},
    stdio: 'ignore', windowsHide: true});
  await waitReady(child as any, `http://127.0.0.1:${port}/health/live`, token, 20000);
  gateway = {child, port, token};
}
function pythonExecutable(): string {
  if (fs.existsSync(bundled().python)) return bundled().python;
  if (app.isPackaged) throw new Error('Bundled Python runtime is missing. Reinstall Bucker Desktop.');
  const devPython = path.join(appRoot(), '.venv', 'Scripts', 'python.exe');
  return fs.existsSync(devPython) ? devPython : 'python';
}
function appRoot(): string {
  return app.isPackaged ? path.dirname(app.getPath('exe')) : path.resolve(__dirname, '../../..');
}
function stopGateway() { gateway?.child.kill(); gateway = null; }

async function hermesExecutable(): Promise<string> {
  if (fs.existsSync(bundled().python)) return bundled().python;
  if (app.isPackaged) throw new Error('Bundled agent runtime is missing. Reinstall Bucker Desktop.');
  if (state.hermesPath && fs.existsSync(state.hermesPath)) return state.hermesPath;
  const target = process.platform === 'win32' ? 'hermes.exe' : 'hermes';
  return new Promise(resolve => {
    const child = spawn('where' + (process.platform === 'win32' ? '' : '') , [target], {stdio: 'pipe', windowsHide: true, shell: process.platform !== 'win32'});
    let stdout = '';
    child.stdout?.on('data', chunk => { stdout += String(chunk); });
    child.once('error', () => resolve(''));
    child.once('exit', code => resolve(code === 0 ? (stdout.split(/\r?\n/).find(l => l.trim().toLowerCase().endsWith(target)) ?? '') : ''));
  });
}

async function connectAcp(): Promise<void> {
  if (!workspace) throw new Error('Open a folder first');
  if (acp) return;
  if (!state.connections.length) throw new Error('Connect a provider in Settings first.');
  await startGateway();
  if (!gateway) throw new Error('No valid provider models are configured.');
  const exe = await hermesExecutable();
  if (!exe) throw new Error('The agent runtime is missing. Reinstall Bucker Desktop, or select the executable manually.');
  const hermesHome = path.join(appHome(), 'hermes-home');
  fs.mkdirSync(hermesHome, {recursive: true});
  await writeHermesConfig(hermesHome);
  const child = (require('node:child_process').spawn as typeof import('node:child_process').spawn)(exe, exe === bundled().python ? [bundled().launcher, 'acp'] : ['acp'], {
    cwd: workspace.root, env: {...sidecarEnv(process.env, hermesHome), BUCKER_GATEWAY_URL: gateway ? `http://127.0.0.1:${gateway.port}/v1` : '', BUCKER_GATEWAY_TOKEN: gateway?.token ?? ''}, stdio: 'pipe', windowsHide: true});
  const client = new AcpClient(child);
  acp = client;
  client.on('closed', reason => { if (acp === client) { acp = null; sessionId = null; busy = false; pushEvent({type: 'error', message: reason}); pushEvent({type: 'status'}); } });
  try {
    sessionId = await startSession(client, workspace.root, state.workspace ? state.sessionByWorkspace[state.workspace] ?? null : null, pushEvent);
    if (state.workspace) state.sessionByWorkspace[state.workspace] = sessionId;
    persist();
  } catch (error) {
    client.close('Agent session setup failed');
    throw error;
  }
  pushEvent({type: 'status'});
}
async function writeHermesConfig(home: string) {
  if (!gateway) return;
  const config = {model: {provider: 'custom', default: 'desktop-auto', api_mode: 'chat_completions', base_url: `http://127.0.0.1:${gateway.port}/v1`, api_key: gateway.token}};
  await fsPromises.writeFile(path.join(home, 'config.yaml'), JSON.stringify(config), 'utf8');
}

const handlers: Record<string, (payload: any) => Promise<any>> = {
  status: async () => publicStatus(),
  openFolder: async () => {
    const result = win ? await dialog.showOpenDialog(win, {properties: ['openDirectory']}) : null;
    if (!result || result.canceled || !result.filePaths[0]) return publicStatus();
    state.workspace = result.filePaths[0]; workspace = new Workspace(result.filePaths[0]);
    acp?.close(); acp = null; sessionId = state.sessionByWorkspace[state.workspace] ?? null; busy = false;
    persist(); return publicStatus();
  },
  tree: async payload => (workspace ? workspace.tree(payload?.path ?? '') : []),
  read: async payload => { if (!workspace) throw new Error('No folder open'); return workspace.read(payload.path); },
  save: async payload => { if (!workspace) throw new Error('No folder open'); return workspace.save(payload.path, payload.content, payload.version); },
  chooseHermes: async () => {
    const result = win ? await dialog.showOpenDialog(win, {properties: ['openFile'], filters: [{name: 'Executable', extensions: process.platform === 'win32' ? ['exe'] : ['*']}]}) : null;
    if (result && !result.canceled && result.filePaths[0]) { state.hermesPath = result.filePaths[0]; persist(); }
    return publicStatus();
  },
  connect: async () => { await connectAcp(); return publicStatus(); },
  prompt: async payload => {
    if (!acp || !sessionId) throw new Error('Not connected');
    if (busy) throw new Error('The agent is already working. Stop or wait for it to finish.');
    busy = true; pushEvent({type: 'status'});
    try {
      const result = await acp.request('session/prompt', {sessionId, prompt: [{type: 'text', text: String(payload.text)}]}, 1800000);
      return {stopReason: result.stopReason ?? 'unknown'};
    } finally { busy = false; pushEvent({type: 'status'}); }
  },
  cancel: async () => { if (acp && sessionId) cancelSession(acp, sessionId); return {ok: true}; },
  permission: async payload => { acp?.respondPermission(payload.id, payload.optionId); return {ok: true}; },
  providers: async () => Object.entries(PROVIDERS).map(([id, p]) => ({id, ...p, connected: state.connections.some(c => c.provider === id)})),
  saveConnection: async payload => {
    const provider = PROVIDERS[payload.provider];
    if (!provider) throw new Error('Unknown provider');
    if (!provider.models.some(model => model.id === payload.model)) throw new Error('Select a catalog model.');
    if (!safeStorage.isEncryptionAvailable()) throw new Error('OS credential encryption is unavailable.');
    if (typeof payload.apiKey !== 'string' || payload.apiKey.length < 16) throw new Error('That API key looks too short to be valid.');
    if (provider.tier === 'free' && payload.provider === 'openrouter' && !String(payload.model).endsWith(':free')) throw new Error('Only OpenRouter models tagged :free are allowed.');
    if (payload.freeAccountConfirmed !== true) throw new Error('Confirm the account billing status before connecting.');
    state.connections = [...state.connections.filter(c => c.provider !== payload.provider), {provider: payload.provider, model: String(payload.model), apiKey: payload.apiKey.trim()}];
    acp?.close('Provider configuration changed; reconnect the agent.');
    persist(); stopGateway(); await startGateway();
    return publicStatus();
  },
  removeConnection: async payload => {
    state.connections = state.connections.filter(c => c.provider !== payload.provider);
    acp?.close('Provider configuration changed; reconnect the agent.');
    persist(); stopGateway(); await startGateway();
    return publicStatus();
  },
  providerLink: async payload => { const provider = PROVIDERS[payload.provider]; if (provider) await shell.openExternal(provider.signup); return {ok: true}; },
  resetSession: async () => { if (state.workspace) { delete state.sessionByWorkspace[state.workspace]; persist(); } acp?.close(); acp = null; sessionId = null; return publicStatus(); },
};

function registerIpc() {
  for (const [command, handler] of Object.entries(handlers)) {
    ipcMain.handle(`desktop:${command}`, async (_event, payload) => {
      try { return {ok: true, data: await handler(payload)}; }
      catch (error: any) { return {ok: false, error: String(error?.message ?? error).slice(0, 400)}; }
    });
  }
}

function createWindow() {
  win = new BrowserWindow({width: 1440, height: 920, minWidth: 980, show: true, autoHideMenuBar: true, title: 'Bucker Desktop',
    webPreferences: {preload: path.join(__dirname, '../preload/index.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false}});
  win.webContents.setWindowOpenHandler(({url}) => ({action: 'deny'}));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.on('closed', () => { win = null; });
  win.webContents.on('render-process-gone', (_e, details) => pushEvent({type: 'error', message: `Renderer exited: ${details.reason}`}));
  void win.loadFile(bundled().renderer);
}

void app.whenReady().then(() => {
  restore(); registerIpc();
  if (state.workspace && fs.existsSync(state.workspace)) workspace = new Workspace(state.workspace);
  void startGateway().catch(() => {});
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('before-quit', () => { try { acp?.close(); } catch {} stopGateway(); try { persist(); } catch {} });
app.on('window-all-closed', () => app.quit());
