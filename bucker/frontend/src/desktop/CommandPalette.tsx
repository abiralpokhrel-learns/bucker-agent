import React, {useEffect, useRef, useState} from 'react';
import {Search, X} from 'lucide-react';

export type Command = {id: string; label: string; shortcut?: string; disabled?: boolean; run: () => void};

export function CommandPalette({commands, onClose}: {commands: Command[]; onClose: () => void}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState('');
  const [index, setIndex] = useState(0);
  const matches = commands.filter(c => c.label.toLowerCase().includes(query.trim().toLowerCase()));
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { previous?.focus(); };
  }, []);
  useEffect(() => { ref.current?.querySelectorAll('[role=option]')[index]?.scrollIntoView({block:'nearest'}); }, [index]);
  const choose = (command?: Command) => {
    if (!command || command.disabled) return;
    onClose();
    // Close the modal before a command opens another dialog or focuses chat.
    requestAnimationFrame(command.run);
  };
  return <dialog ref={ref} className="command-palette" aria-label="Command palette" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <div className="command-search"><Search size={16}/><input autoFocus aria-label="Search commands" role="textbox" aria-controls="command-results" aria-activedescendant={matches[index] ? `command-${matches[index].id}` : undefined} value={query} placeholder="Type a command…" onChange={e=>{setQuery(e.target.value);setIndex(0);}} onKeyDown={e=>{
      if (e.key==='ArrowDown' || e.key==='ArrowUp') { e.preventDefault(); setIndex(i=>matches.length ? (i+(e.key==='ArrowDown'?1:-1)+matches.length)%matches.length : 0); }
      if (e.key==='Enter') { e.preventDefault(); choose(matches[index]); }
    }}/><button className="icon-button" aria-label="Close command palette" onClick={onClose}><X size={16}/></button></div>
    <div id="command-results" role="listbox" aria-label="Commands">{matches.map((c,i)=><button id={`command-${c.id}`} key={c.id} role="option" aria-selected={i===index} aria-disabled={c.disabled} onMouseMove={()=>setIndex(i)} onClick={()=>choose(c)}><span>{c.label}</span>{c.shortcut && <kbd>{c.shortcut}</kbd>}</button>)}{!matches.length && <p className="subtle-note">No matching commands.</p>}</div>
    <div className="command-help">↑ ↓ to navigate · Enter to run · Escape to close</div>
  </dialog>;
}

export function useEditorPreference(key: string, fallback = false) {
  const [value, setValue] = useState(() => {
    try { const saved = localStorage.getItem(`bucker.editor.${key}`); return saved === null ? fallback : saved === 'true'; }
    catch { return fallback; }
  });
  useEffect(() => { try { localStorage.setItem(`bucker.editor.${key}`, String(value)); } catch { /* Editor still works when storage is unavailable. */ } }, [key, value]);
  return [value, setValue] as const;
}
