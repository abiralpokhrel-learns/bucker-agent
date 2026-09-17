import React from 'react';
import { TopBar } from './components/layout/TopBar';
import { Sidebar } from './components/layout/Sidebar';
import { CodeEditor } from './components/editor/CodeEditor';
import { DiffViewer } from './components/editor/DiffViewer';
import { Terminal } from './components/terminal/Terminal';
import { ChatPanel } from './components/chat/ChatPanel';
import { useAppStore } from './store/appStore';

export const App: React.FC = () => {
  const { activeDiffTaskId } = useAppStore();

  return (
    <div className="flex flex-col h-screen w-screen bg-[#0d1117] text-[#e6edf3] overflow-hidden">
      {/* Navigation Top Bar */}
      <TopBar />

      {/* Main Workspace Workspace */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Activity Bar + Drawer */}
        <Sidebar />

        {/* Central Editor & Terminal Area */}
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Editor or Diff View */}
          <div className="flex-1 overflow-hidden relative">
            {activeDiffTaskId ? <DiffViewer /> : <CodeEditor />}
          </div>

          {/* Collapsible Bottom Terminal */}
          <Terminal />
        </div>

        {/* Right AI Agent Cockpit */}
        <ChatPanel />
      </div>
    </div>
  );
};
