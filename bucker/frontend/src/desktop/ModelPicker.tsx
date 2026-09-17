import React, {useState} from 'react';
import {Search, X} from 'lucide-react';
import type {ProviderInfo} from './bridge';

type Props = {models: ProviderInfo['models']; value: string; onChange: (id: string) => void; savedModels: string[]};

export function ModelPicker({models, value, onChange, savedModels}: Props) {
  const [query, setQuery] = useState('');
  const filtered = models.filter(m => m.id.toLowerCase().includes(query.trim().toLowerCase()));
  const selected = models.find(m => m.id === value);
  const selectionVisible = filtered.some(m => m.id === value);
  return <>
    <label htmlFor="model-search">Search models</label>
    <div className="model-search"><Search size={14}/><input id="model-search" type="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Filter by model name…"/>{query && <button className="icon-button" aria-label="Clear model search" onClick={()=>setQuery('')}><X size={14}/></button>}</div>
    <label htmlFor="provider-model">Model <span className="model-count">{filtered.length} of {models.length}</span></label>
    <select id="provider-model" value={selectionVisible ? value : ''} disabled={!filtered.length} onChange={e=>onChange(e.target.value)}>
      {!selectionVisible && <option value="" disabled>{filtered.length ? 'Select a search result' : 'No matching models'}</option>}
      {filtered.map(m=><option key={m.id} value={m.id}>{m.id}{savedModels.includes(m.id) ? ' · saved' : ''}</option>)}
    </select>
    {!filtered.length && <p role="status" className="model-empty">No models match your search.</p>}
    {selected && <div className="model-details"><strong>Selected: {selected.id}</strong><span>{selected.context.toLocaleString('en-US')} context tokens · {selected.maxOutput.toLocaleString('en-US')} max output tokens</span><small>Catalog limits, not remaining quota. Provider availability may change.</small></div>}
    {!!savedModels.length && <p className="saved-models">Saved models: {savedModels.join(', ')}</p>}
  </>;
}
