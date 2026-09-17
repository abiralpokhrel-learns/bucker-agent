import React, {useEffect, useMemo, useRef, useState} from 'react';
import Editor, {loader} from '@monaco-editor/react';
import * as monaco from 'monaco-editor';
import EditorWorker from 'monaco-editor/editor/editor.worker?worker&inline';
import JsonWorker from 'monaco-editor/language/json/json.worker?worker&inline';
import CssWorker from 'monaco-editor/language/css/css.worker?worker&inline';
import HtmlWorker from 'monaco-editor/language/html/html.worker?worker&inline';
import TsWorker from 'monaco-editor/language/typescript/ts.worker?worker&inline';

(self as any).MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    if (label === 'json') return new JsonWorker();
    if (['css', 'scss', 'less'].includes(label)) return new CssWorker();
    if (['html', 'handlebars', 'razor'].includes(label)) return new HtmlWorker();
    if (['typescript', 'javascript'].includes(label)) return new TsWorker();
    return new EditorWorker();
  },
};

import {call} from './bridge';
import {CommandPalette, useEditorPreference} from './CommandPalette';
import {ModelPicker} from './ModelPicker';
import type {AgentUpdate, FileEntry, PermissionRequest, ProviderInfo, Status} from './bridge';
import {FolderOpen, RefreshCw, Save, Send, Square, X, Settings, Plug, File as FileIcon, Folder as FolderIcon, ChevronRight, ChevronDown, Bot, Loader2, ShieldAlert, Check, PanelLeft, Terminal, Code2, ArrowUpRight, Sparkles, ArrowUp} from 'lucide-react';

loader.config({monaco});

type ChatEntry = {role: 'user' | 'assistant' | 'tool' | 'plan' | 'system'; text: string; tool?: string; status?: string};
type Tab = {path: string; name: string; content: string; version: string; dirty: boolean; language: string};

const languageOf = (path: string) => {
  const extension = path.split('.').pop()?.toLowerCase() ?? '';
  const map: Record<string, string> = {py:'python', ts:'typescript', tsx:'typescript', js:'javascript', jsx:'javascript', md:'markdown', json:'json', css:'css', html:'html', go:'go', rs:'rust', yml:'yaml', yaml:'yaml', sh:'shell', toml:'toml'};
  return map[extension] ?? 'plaintext';
};

export const DesktopApp: React.FC = () => {
  const [status, setStatus] = useState<any>(null);
  const [tree, setTree] = useState<FileEntry[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [plan, setPlan] = useState<{content: string; status: string}[]>([]);
  const [permission, setPermission] = useState<PermissionRequest | null>(null);
  const [input, setInput] = useState('');
  const [busyLocal, setBusyLocal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [wordWrap, setWordWrap] = useEditorPreference('wordWrap');
  const [minimap, setMinimap] = useEditorPreference('minimap');
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [keyDraft, setKeyDraft] = useState('');
  const [modelDraft, setModelDraft] = useState<Record<string, string>>({});
  const [freeConfirmed, setFreeConfirmed] = useState(false);
  const [explorerOpen, setExplorerOpen] = useState(true);
  const [activityOpen, setActivityOpen] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState('');
  const [savingProvider, setSavingProvider] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const settingsRef = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (settingsOpen) settingsRef.current?.showModal(); else { setKeyDraft(''); setFreeConfirmed(false); } }, [settingsOpen]);
  useEffect(() => { if (providers.length && !providers.some(p=>p.id===selectedProvider)) setSelectedProvider(providers[0].id); }, [providers, selectedProvider]);
  useEffect(() => { const handler=(e:KeyboardEvent)=>{ if ((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='b') {e.preventDefault();setExplorerOpen(v=>!v);} }; window.addEventListener('keydown',handler);return()=>window.removeEventListener('keydown',handler); }, []);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'p' && !settingsOpen) {
        e.preventDefault(); e.stopPropagation(); setPaletteOpen(v => !v);
      }
    };
    window.addEventListener('keydown', handler, true);
    return () => window.removeEventListener('keydown', handler, true);
  }, [settingsOpen]);
  const chatEndRef = React.useRef<HTMLDivElement>(null);

  const refreshStatus = async () => { try { setStatus(await call('status')); } catch (e: any) { setError(e.message); } };
  useEffect(() => { void refreshStatus(); const unsubscribe = window.desktop.onEvent(event => {
    if (event.type === 'status') { void refreshStatus(); return; }
    if (event.type === 'error') { setError(event.message); return; }
    if (event.type === 'permission') { setPermission({id: event.id, toolCall: event.toolCall, options: event.options ?? []}); return; }
    if (event.type === 'update') applyUpdate(event as AgentUpdate);
  });
    return () => { unsubscribe(); };
  }, []);

  useEffect(() => { if (settingsOpen) void call<ProviderInfo[]>('providers').then(setProviders).catch(e=>setError(e.message)); }, [settingsOpen]);
  useEffect(() => { if (status?.workspace) void loadTree(); }, [status?.workspace]);
  useEffect(() => { chatEndRef.current?.scrollIntoView({behavior:'smooth'}); }, [entries]);

  const applyUpdate = (event: AgentUpdate) => {
    const update = event.update ?? {};
    const kind = update.sessionUpdate ?? update['session_update'];
    if (kind === 'agent_message_chunk') {
      const text = (update.content?.text) ?? '';
      setEntries(prev => {
        const last = prev[prev.length-1];
        if (last && last.role === 'assistant') return [...prev.slice(0,-1), {...last, text: last.text + text}];
        return [...prev, {role:'assistant', text}];
      });
    } else if (kind === 'tool_call' || kind === 'tool_call_update') {
      const call_ = update;
      const output = (call_.content ?? []).map((c: any) => c.type === 'content' ? (c.content?.text ?? '') : (c.type === 'diff' ? `diff ${c.path ?? ''}` : '')).filter(Boolean).join('\n');
      setEntries(prev => {
        const index = prev.findIndex(e => e.role === 'tool' && e.text.startsWith(call_.toolCallId ?? ''));
        const text = `${call_.toolCallId ?? ''}|${call_.title ?? kind}`;
        const entry: ChatEntry = {role:'tool', text, tool: output || undefined, status: call_.status};
        if (index >= 0) { const next = [...prev]; next[index] = {...entry, tool: output || prev[index].tool}; return next; }
        return [...prev, entry];
      });
    } else if (kind === 'plan') {
      setPlan((update.entries ?? []).map((e: any) => ({content: e.content, status: e.status})));
    } else if (kind === 'user_message_chunk') {
      const text = update.content?.text ?? '';
      setEntries(prev => prev.some(e => e.role==='user' && e.text===text) ? prev : [...prev, {role:'user', text}]);
    }
  };

  const loadTree = async () => { if (!status?.workspace) return; try { setTree(await call('tree', {})); } catch (e:any) { setError(e.message); } };
  const openFolder = async () => {
    if (busyLocal || status?.busy || tabs.some(t => t.dirty)) { setError('Save your edits and stop the agent before changing folders.'); return; }
    try { const next = await call<Status>('openFolder'); if (next.workspace !== status?.workspace) { setTabs([]); setActivePath(null); setEntries([]); setPlan([]); setExpanded({}); setPermission(null); } setStatus(next); } catch (e:any) { setError(e.message); }
  };
  const openFile = async (entry: FileEntry) => {
    if (entry.type === 'directory') { setExpanded(e => ({...e, [entry.path]: !e[entry.path]})); return; }
    if (tabs.some(t => t.path === entry.path)) { setActivePath(entry.path); return; }
    try { const data = await call<{path:string;content:string;version:string}>('read', {path: entry.path});
      setTabs(prev => [...prev, {path: data.path, name: entry.name, content: data.content, version: data.version, dirty: false, language: languageOf(data.path)}]);
      setActivePath(data.path);
    } catch (e:any) { setError(e.message); }
  };
  const saveActive = async () => {
    const tab = tabs.find(t => t.path === activePath); if (!tab) return;
    try { const result = await call<{version:string}>('save', {path: tab.path, content: tab.content, version: tab.version});
      setTabs(prev => prev.map(t => t.path===tab.path ? {...t, version: result.version, dirty:false} : t));
    } catch (e:any) { setError(e.message); }
  };
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='s') { e.preventDefault(); void saveActive(); } };
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  });

  const connect = async () => { if(connecting)return; setConnecting(true); setError(null); try { await call('connect'); await refreshStatus(); } catch (e:any) { setError(e.message); } finally {setConnecting(false);} };
  const sendPrompt = async () => {
    const text = input.trim(); if (!text || busyLocal || !status?.connected) return;
    setEntries(prev => [...prev, {role:'user', text}]); setInput(''); setBusyLocal(true); setError(null);
    try { const result = await call<{stopReason:string}>('prompt', {text});
      if (result?.stopReason && result.stopReason !== 'end_turn') setEntries(prev => [...prev, {role:'system', text:`Stopped: ${result.stopReason}`}]);
    } catch (e:any) { setError(e.message); } finally { setBusyLocal(false); }
  };
  const cancel = async () => { try { await call('cancel'); } catch (e:any) { setError(e.message); } };
  const answerPermission = async (optionId: string | null) => {
    if (!permission) return;
    try { await call('permission', {id: permission.id, optionId}); } catch (e:any) { setError(e.message); }
    setPermission(null);
  };
  const saveConnection = async (providerId: string) => {
    if(savingProvider || !keyDraft.trim() || !freeConfirmed)return;
    setSavingProvider(true);
    setError(null);
    try { await call('saveConnection', {provider: providerId, model: modelDraft[providerId] ?? providers.find(p=>p.id===providerId)?.models[0]?.id ?? '', apiKey: keyDraft.trim(), freeAccountConfirmed: freeConfirmed});
      setKeyDraft(''); setFreeConfirmed(false); setProviders(await call('providers')); await refreshStatus();
    } catch (e:any) { setError(e.message); } finally {setSavingProvider(false);}
  };

  if (!window.desktop) return <div className="p-8 text-sm text-slate-300">This build must run inside the Bucker Desktop app (electron).</div>;
  const activeTab = tabs.find(t => t.path === activePath);
  const busy = busyLocal || status?.busy;

  return (
    <div className="bucker-shell">
      <header className="app-toolbar">
        <div className="brand"><span className="brand-mark">b.</span><strong>Bucker</strong><span className="preview-label">DESKTOP</span></div>
        <button className="workspace-switch" onClick={openFolder} title={status?.workspace || 'Open a project folder'}><FolderOpen size={16}/><span>{status?.workspace ? status.workspace.split(/[\\/]/).pop() : 'Open folder'}</span><ChevronDown size={13}/></button>
        <div className="toolbar-spacer"/>
        <button className="command-trigger" title="Command palette (Ctrl+Shift+P)" onClick={()=>setPaletteOpen(true)}>Commands <kbd>Ctrl Shift P</kbd></button>
        <button className="connection-button" onClick={()=>setSettingsOpen(true)}><span className={`status-dot ${status?.connected ? 'online' : ''}`}/>{status?.connected ? 'Agent connected' : 'Set up AI'}</button>
        <span className="toolbar-divider"/>
        <button className="icon-button" aria-label="Toggle Explorer" title="Toggle Explorer (Ctrl+B)" onClick={()=>setExplorerOpen(v=>!v)} aria-pressed={explorerOpen}><PanelLeft size={17}/></button>
        <button className="icon-button" aria-label="Toggle activity" title="Toggle activity" onClick={()=>setActivityOpen(v=>!v)} aria-pressed={activityOpen}><Terminal size={17}/></button>
        <button className="icon-button" title="Providers" aria-label="Providers" onClick={()=>setSettingsOpen(v=>!v)}><Settings size={17}/></button>
      </header>
      {error && <div role="alert" className="error-banner"><ShieldAlert size={16}/><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={()=>setError(null)}><X size={15}/></button></div>}
      <div className="workspace-body">
        {explorerOpen && <aside className="explorer-pane" aria-label="Project files">
          <div className="panel-heading"><span>EXPLORER</span><button className="icon-button" title="Refresh files" aria-label="Refresh files" onClick={loadTree}><RefreshCw size={14}/></button></div>
          {status?.workspace ? <><div className="project-label"><ChevronDown size={13}/><strong>{status.workspace.split(/[\\/]/).pop()}</strong></div><div className="file-list"><FileRows entries={tree} expanded={expanded} onToggle={openFile} activePath={activePath} level={0}/>{!tree.length && <p className="subtle-note">This folder is empty.</p>}</div></> : <div className="explorer-empty"><FolderOpen size={27}/><strong>Your project lives here</strong><p>Open a folder to browse and edit its files.</p><button className="secondary-button" onClick={openFolder}>Choose folder</button></div>}
          <div className="explorer-footer"><ShieldAlert size={14}/><span>Files stay on your device</span></div>
        </aside>}
        <main className="workspace-pane">
          <div className="editor-tabs" role="tablist" aria-label="Open files">
            {!tabs.length && <div className="welcome-tab"><Code2 size={15}/> Getting started</div>}
            {tabs.map(t=><div key={t.path} className={`editor-tab ${activePath===t.path?'active':''}`}><button role="tab" aria-selected={activePath===t.path} title={t.path} onClick={()=>setActivePath(t.path)}><FileIcon size={14}/><span>{t.name}</span>{t.dirty && <span className="dirty-dot" aria-label="Unsaved changes"/>}</button><button className="tab-close" aria-label={`Close ${t.name}`} onClick={()=>{if(t.dirty){setError('Save this file before closing its tab.');return;}setTabs(ts=>ts.filter(x=>x.path!==t.path));if(activePath===t.path)setActivePath(tabs.find(x=>x.path!==t.path)?.path??null);}}><X size={13}/></button></div>)}
          </div>
          {activeTab && <div className="file-breadcrumb"><span>{activeTab.path}</span><div className="editor-actions"><button className="editor-option" aria-label="Toggle word wrap" title="Toggle word wrap" aria-pressed={wordWrap} onClick={()=>setWordWrap(v=>!v)}>Wrap</button><button className="editor-option" aria-label="Toggle minimap" title="Toggle minimap" aria-pressed={minimap} onClick={()=>setMinimap(v=>!v)}>Minimap</button><button className="save-button" disabled={!activeTab.dirty} onClick={saveActive}><Save size={13}/>{activeTab.dirty?'Save changes':'Saved'}<kbd>Ctrl S</kbd></button></div></div>}
          <div className="editor-surface">
            {activeTab ? <Editor height="100%" path={activeTab.path} language={activeTab.language} value={activeTab.content} theme="bucker-night" beforeMount={m=>m.editor.defineTheme('bucker-night',{base:'vs-dark',inherit:true,rules:[],colors:{'editor.background':'#1f1f1f','editor.foreground':'#cccccc','editorLineNumber.foreground':'#6e7681','editorLineNumber.activeForeground':'#cccccc','editor.selectionBackground':'#264f78','editor.lineHighlightBackground':'#ffffff0a','editorCursor.foreground':'#aeafad','editorWidget.background':'#252526'}})} onChange={value=>setTabs(ts=>ts.map(t=>t.path===activeTab.path?{...t,content:value??'',dirty:true}:t))} options={{fontSize:14,lineHeight:23,fontFamily:'Cascadia Code, Consolas, monospace',padding:{top:18,bottom:18},minimap:{enabled:minimap},wordWrap:wordWrap?'on':'off',automaticLayout:true,scrollBeyondLastLine:false,smoothScrolling:true,renderLineHighlight:'all',bracketPairColorization:{enabled:true}}}/> :
            <div className="welcome-screen"><div className="welcome-content"><span className="welcome-eyebrow"><span className="tiny-mark">b.</span> YOUR NEXT IDEA STARTS HERE</span><h1>A little context.<br/>A lot of possibility.</h1><p className="welcome-description">Your files, a focused editor, and an AI partner.<br/>One space to build something that matters.</p>
              <div className="welcome-actions"><button className="primary-button" onClick={openFolder}><FolderOpen size={17}/>{status?.workspace?'Switch project':'Open a project'}<ArrowUpRight size={15}/></button><button className="secondary-button" onClick={()=>setSettingsOpen(true)}><Plug size={16}/>Connect an AI provider</button></div>
              <div className="setup-steps"><div className={status?.workspace?'done':''}><span>{status?.workspace?<Check size={14}/>:'1'}</span><strong>Bring your project</strong><small>Choose a local folder</small></div><div className={status?.connections?.length?'done':''}><span>{status?.connections?.length?<Check size={14}/>:'2'}</span><strong>Connect your AI</strong><small>Use your own provider key</small></div><div className={status?.connected?'done':''}><span>{status?.connected?<Check size={14}/>:'3'}</span><strong>Build together</strong><small>Describe your next change</small></div></div>
              {status?.workspace && <div className="project-ready"><Check size={15}/><span>Project open. Select a file in Explorer to start editing.</span></div>}
              <div className="welcome-shortcuts"><span><kbd>Ctrl S</kbd> Save file</span><span><kbd>Ctrl B</kbd> Toggle Explorer</span><span><kbd>Shift ↵</kbd> New line in chat</span></div>
            </div><div className="welcome-footnote">BUCKER DESKTOP <span>·</span> YOUR PROJECT, YOUR CONTROL</div></div>}
          </div>
          {activityOpen && <section className="activity-panel" aria-label="Agent activity"><div className="panel-heading"><span><Terminal size={14}/> ACTIVITY <span className="count-badge">{entries.filter(e=>e.role==='tool').length}</span></span><button className="icon-button" aria-label="Close activity" onClick={()=>setActivityOpen(false)}><X size={14}/></button></div><div className="activity-scroll">{!entries.some(e=>e.role==='tool') && <div className="activity-empty"><Terminal size={20}/><div><strong>Your tool output, in one place.</strong><p>Commands and results appear here when the agent works.</p></div></div>}{entries.filter(e=>e.role==='tool').map(e=><details className="tool-result" key={e.text}><summary><span className={`status-dot ${e.status==='completed'?'online':''}`}/><span>{e.text.split('|')[1]}</span><small>{e.status??'running'}</small></summary><pre>{e.tool || 'Waiting for output…'}</pre></details>)}</div></section>}
        </main>
        <aside className="assistant-pane" aria-label="Bucker assistant">
          <div className="assistant-heading"><span className="agent-avatar"><Bot size={20}/></span><div><strong>Bucker</strong><span>Your coding partner</span></div><span className="agent-label">AGENT</span></div>
          <div className="assistant-session"><span className={`status-dot ${status?.connected?'online':''}`}/><span>{busy?'Working on your request':status?.connected?'Ready when you are':'Not connected'}</span>{status?.workspace && !status?.connected && <button onClick={()=>status?.connections?.length?void connect():setSettingsOpen(true)} disabled={connecting}>{connecting?'Connecting…':'Connect'}</button>}</div>
          {plan.length>0 && <div className="plan-card"><strong>Plan <span>{plan.filter(p=>p.status==='completed').length}/{plan.length}</span></strong>{plan.map((p,i)=><div key={i} className={p.status==='completed'?'complete':''}>{p.status==='completed'?<Check size={14}/>:<span className="plan-index">{i+1}</span>}<span>{p.content}</span></div>)}</div>}
          <div className="conversation" aria-live="polite">
            {!entries.some(e=>e.role!=='tool') && <div className="assistant-empty"><span className="assistant-spark"><Sparkles size={24}/></span><h2>What are we building?</h2><p>{status?.connected?'Describe a change, ask a question, or work through an idea.':'Connect an AI provider to plan changes and work with your code.'}</p><div className="prompt-suggestions">{['Explain this project','Help me find and fix a bug','Plan my next feature'].map(text=><button key={text} onClick={()=>{setInput(text);composerRef.current?.focus();}}><span>{text}</span><ArrowUpRight size={14}/></button>)}</div><small>Suggestions fill the composer. Nothing runs until you send.</small></div>}
            {entries.filter(e=>e.role!=='tool').map((e,i)=><article className={`message ${e.role}`} key={i}><div className="message-author">{e.role==='user'?'YOU':e.role==='system'?'NOTICE':'BUCKER'}</div><div className="message-text">{e.text}</div></article>)}
            {busy && <div className="thinking"><Loader2 size={14} className="animate-spin"/> Working… <button onClick={cancel}>Stop</button></div>}<div ref={chatEndRef}/>
          </div>
          {permission && <section className="permission-card" aria-label="Permission requested"><h3><ShieldAlert size={17}/> Your approval is needed</h3><p>{permission.toolCall?.title??'The agent wants to run an action'}</p><div>{permission.options.map(o=><button className="secondary-button" key={o.optionId} onClick={()=>answerPermission(o.optionId)}>{o.name}</button>)}<button className="text-button" onClick={()=>answerPermission(null)}>Dismiss</button></div></section>}
          <div className="composer-wrap"><div className="composer"><textarea ref={composerRef} aria-label="Message Bucker" value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();void sendPrompt();}}} rows={3} placeholder="Ask Bucker to build, explain, or fix…"/><div className="composer-toolbar"><span><Code2 size={13}/>{status?.workspace?'Project context':'No project open'}</span>{busy?<button className="send-button stop" title="Stop agent" aria-label="Stop agent" onClick={cancel}><Square size={15}/></button>:<button className="send-button" title="Send message" aria-label="Send message" onClick={sendPrompt} disabled={!status?.connected||!input.trim()}><ArrowUp size={17}/></button>}</div></div><p className="composer-hint">{status?.connected?'Enter to send · Shift+Enter for a new line':!status?.workspace?'Open a project, then connect your AI to start.':'Connect your AI to send a message.'}</p></div>
        </aside>
      </div>
      <footer className="statusbar"><span><span className={`status-dot ${status?.connected?'online':''}`}/>{status?.connected?'Agent connected':'Editor ready'}</span><span className="status-safety"><ShieldAlert size={12}/>Local execution · not sandboxed</span><div className="toolbar-spacer"/><span>{tabs.filter(t=>t.dirty).length?`${tabs.filter(t=>t.dirty).length} unsaved`:'All changes saved'}</span>{activeTab && <><span>UTF-8</span><span>{activeTab.language}</span></>}</footer>
      {paletteOpen && <CommandPalette onClose={()=>setPaletteOpen(false)} commands={[
        {id:'save', label:'File: Save active file', shortcut:'Ctrl S', disabled:!activeTab?.dirty, run:()=>void saveActive()},
        {id:'folder', label:'File: Open folder', run:()=>void openFolder()},
        {id:'wrap', label:`Editor: Toggle word wrap (${wordWrap?'on':'off'})`, run:()=>setWordWrap(v=>!v)},
        {id:'minimap', label:`Editor: Toggle minimap (${minimap?'on':'off'})`, run:()=>setMinimap(v=>!v)},
        {id:'explorer', label:'View: Toggle Explorer', shortcut:'Ctrl B', run:()=>setExplorerOpen(v=>!v)},
        {id:'activity', label:'View: Toggle agent activity', run:()=>setActivityOpen(v=>!v)},
        {id:'providers', label:'Preferences: AI providers', run:()=>setSettingsOpen(true)},
        {id:'chat', label:'Agent: Focus message composer', run:()=>composerRef.current?.focus()},
      ]}/>}
      {settingsOpen && <dialog ref={settingsRef} className="provider-dialog" aria-labelledby="provider-title" onCancel={()=>setSettingsOpen(false)} onClick={e=>{if(e.target===e.currentTarget)setSettingsOpen(false);}}><div className="settings-header"><div><span className="section-eyebrow">MAKE IT YOURS</span><h2 id="provider-title">Connect your AI</h2><p>Your provider. Your key. Choose one to get started.</p></div><button className="icon-button" aria-label="Close provider settings" onClick={()=>setSettingsOpen(false)}><X size={20}/></button></div><div className="settings-layout"><nav className="provider-list" aria-label="AI providers">{providers.map(p=><button key={p.id} aria-label={p.name} aria-pressed={selectedProvider===p.id} className={selectedProvider===p.id?'selected':''} onClick={()=>{setSelectedProvider(p.id);setKeyDraft('');setFreeConfirmed(false);}}><span className="provider-monogram">{p.name.slice(0,1)}</span><span>{p.name}<small>{p.connected?'Key saved':'Bring your own key'}</small></span>{p.connected?<Check size={14}/>:<ChevronRight size={14}/>}</button>)}{!providers.length && <p className="subtle-note">Loading providers…</p>}</nav><div className="provider-detail">{providers.filter(p=>p.id===selectedProvider).map(p=><React.Fragment key={p.id}><h3>{p.name}</h3><p className="provider-notes">{p.notes}</p><div className="setup-instructions"><span className="step-label">01 / GET YOUR KEY</span><ol>{p.steps.map((s:string)=><li key={s}>{s}</li>)}</ol><button className="secondary-button" onClick={()=>void call('providerLink',{provider:p.id}).catch(e=>setError(e.message))}>Open provider website <ArrowUpRight size={14}/></button></div><div className="connection-form"><span className="step-label">02 / CONNECT TO BUCKER</span><ModelPicker models={p.models} value={modelDraft[p.id]??p.models[0]?.id??''} onChange={id=>setModelDraft(m=>({...m,[p.id]:id}))} savedModels={(status?.connections??[]).filter((c:{provider:string})=>c.provider===p.id).map((c:{model:string})=>c.model)}/><label htmlFor="provider-key">{p.connected?'Replace API key':'API key'}</label><input id="provider-key" type="password" autoComplete="off" value={keyDraft} onChange={e=>setKeyDraft(e.target.value)} placeholder={p.connected?'Enter a new key to replace the saved one':'Paste your provider key'}/><label className="consent"><input type="checkbox" checked={freeConfirmed} onChange={e=>setFreeConfirmed(e.target.checked)}/><span>I understand this provider’s free limits and have checked my account’s billing settings.</span></label><button className="primary-button" disabled={!keyDraft.trim()||!freeConfirmed||savingProvider||!p.models.length} onClick={()=>void saveConnection(p.id)}>{savingProvider?<Loader2 size={15} className="animate-spin"/>:<Plug size={15}/>}Save connection</button>{p.connected && <p className="connection-saved"><Check size={14}/> Key saved securely. This does not verify provider availability.</p>}</div></React.Fragment>)}</div></div><div className="settings-footer"><ShieldAlert size={14}/><span>Keys are encrypted by your OS. Free quotas and billing are controlled by your provider.</span></div></dialog>}
    </div>
  );
};

const FileRows: React.FC<{entries:FileEntry[]; expanded:Record<string,boolean>; onToggle:(e:FileEntry)=>void; activePath:string|null; level?:number}> = ({entries,expanded,onToggle,activePath,level=0}) => <>{entries.map(e=><React.Fragment key={e.path}><button className={`file-row ${activePath===e.path?'selected':''}`} title={e.path} onClick={()=>onToggle(e)} style={{paddingLeft:12+level*14}} aria-expanded={e.type==='directory'?!!expanded[e.path]:undefined}>{e.type==='directory'?<>{expanded[e.path]?<ChevronDown size={12}/>:<ChevronRight size={12}/>}<FolderIcon size={15}/></>:<><span className="file-indent"/><FileIcon size={14}/></>}<span>{e.name}</span></button>{e.type==='directory'&&expanded[e.path]&&<LazyChildren path={e.path} expanded={expanded} onToggle={onToggle} activePath={activePath} level={level+1}/>}</React.Fragment>)}</>;
const LazyChildren: React.FC<{path:string; expanded:Record<string,boolean>; onToggle:(e:FileEntry)=>void; activePath:string|null; level:number}> = ({path,expanded,onToggle,activePath,level}) => {
  const [children,setChildren]=useState<FileEntry[]|null>(null);
  const [loadError,setLoadError]=useState(false);
  useEffect(()=>{let alive=true;call<FileEntry[]>('tree',{path}).then(c=>alive&&setChildren(c)).catch(()=>alive&&setLoadError(true));return()=>{alive=false;};},[path]);
  if(loadError)return <p className="subtle-note">Could not read this folder.</p>;
  if(!children)return <p className="subtle-note">Loading…</p>;
  return <FileRows entries={children} expanded={expanded} onToggle={onToggle} activePath={activePath} level={level}/>;
};
