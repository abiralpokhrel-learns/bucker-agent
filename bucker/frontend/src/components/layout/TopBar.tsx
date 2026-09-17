import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../store/appStore';
import { fetchSystemStatus } from '../../api/client';
import { 
  Terminal as TerminalIcon, 
  Bot, 
  FolderOpen, 
  Activity, 
  PlusCircle, 
  CheckCircle2, 
  AlertCircle 
} from 'lucide-react';

export const TopBar: React.FC = () => {
  const { 
    workspaceRoot, 
    toggleBottomPanel, 
    toggleRightPanel, 
    bottomPanelOpen, 
    rightPanelOpen,
    setActiveSidebarTab,
    selectedTaskId,
    setActiveDiffTaskId
  } = useAppStore();

  const [systemInfo, setSystemInfo] = useState<any>(null);

  useEffect(() => {
    fetchSystemStatus().then(setSystemInfo).catch(() => {});
  }, []);

  return (
    <header className="h-11 bg-[#161b22] border-b border-[#30363d] flex items-center justify-between px-3 text-xs text-[#e6edf3]">
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5 font-bold tracking-tight text-sm text-[#58a6ff]">
          <span>🛡️ bucker-agent</span>
          <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-[#30363d] text-[#8b949e] font-normal">v2.0</span>
        </div>

        <div className="h-4 w-[1px] bg-[#30363d] mx-1" />

        <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-[#0d1117] border border-[#30363d] text-[#8b949e]">
          <FolderOpen size={13} className="text-[#58a6ff]" />
          <span className="truncate max-w-[260px] font-mono text-[11px] text-[#e6edf3]">
            {workspaceRoot}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => setActiveSidebarTab('tasks')}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-[#238636] hover:bg-[#2ea043] text-white font-medium transition-colors"
        >
          <PlusCircle size={13} />
          <span>New Task</span>
        </button>

        <button
          onClick={toggleBottomPanel}
          title="Toggle Terminal (Ctrl+`)"
          className={`p-1.5 rounded hover:bg-[#21262d] border border-transparent transition-colors ${
            bottomPanelOpen ? 'bg-[#21262d] border-[#30363d] text-[#58a6ff]' : 'text-[#8b949e]'
          }`}
        >
          <TerminalIcon size={14} />
        </button>

        <button
          onClick={toggleRightPanel}
          title="Toggle Agent Chat"
          className={`p-1.5 rounded hover:bg-[#21262d] border border-transparent transition-colors ${
            rightPanelOpen ? 'bg-[#21262d] border-[#30363d] text-[#bc8cff]' : 'text-[#8b949e]'
          }`}
        >
          <Bot size={14} />
        </button>

        <div className="h-4 w-[1px] bg-[#30363d] mx-1" />

        <div className="flex items-center gap-1.5 text-[11px] text-[#8b949e]" title="System & Inference Gateway Status">
          <span className="inline-block w-2 h-2 rounded-full bg-[#3fb950]" />
          <span>Gateway Live</span>
        </div>
      </div>
    </header>
  );
};
