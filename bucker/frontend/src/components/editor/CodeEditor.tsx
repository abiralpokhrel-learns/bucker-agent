import React from 'react';
import Editor from '@monaco-editor/react';
import { useAppStore } from '../../store/appStore';
import { writeFile } from '../../api/client';
import { X, Save } from 'lucide-react';

export const CodeEditor: React.FC = () => {
  const { openTabs, activeTabPath, setActiveTabPath, closeTab, updateFileContent } = useAppStore();

  const activeTab = openTabs.find((t) => t.path === activeTabPath);

  const handleSave = async () => {
    if (!activeTab) return;
    try {
      await writeFile(activeTab.path, activeTab.content);
      // Clean dirty flag
      updateFileContent(activeTab.path, activeTab.content);
    } catch (e) {
      console.error('Save failed', e);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      handleSave();
    }
  };

  if (!activeTab) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-[#8b949e] bg-[#0d1117] text-sm">
        <div className="text-4xl mb-3">🛡️</div>
        <p className="font-medium text-[#e6edf3]">Bucker Agent IDE</p>
        <p className="text-xs text-[#8b949e] mt-1">Select a file from the explorer or create a task to begin.</p>
        <div className="flex gap-4 mt-6 text-xs text-[#58a6ff]">
          <span>Ctrl+` : Toggle Terminal</span>
          <span>Ctrl+P : Quick Search</span>
          <span>Ctrl+S : Save File</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-[#0d1117]" onKeyDown={handleKeyDown}>
      {/* Editor Tabs */}
      <div className="flex items-center bg-[#161b22] border-b border-[#30363d] overflow-x-auto text-xs">
        {openTabs.map((tab) => {
          const isActive = tab.path === activeTabPath;
          return (
            <div
              key={tab.path}
              onClick={() => setActiveTabPath(tab.path)}
              className={`flex items-center gap-2 px-3 py-2 border-r border-[#30363d] cursor-pointer select-none transition-colors ${
                isActive
                  ? 'bg-[#0d1117] text-[#e6edf3] border-t-2 border-t-[#58a6ff]'
                  : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e6edf3]'
              }`}
            >
              <span className="truncate max-w-[150px]">{tab.name}</span>
              {tab.isDirty && <span className="w-1.5 h-1.5 rounded-full bg-[#d29922]" title="Unsaved changes" />}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(tab.path);
                }}
                className="hover:text-[#f85149] p-0.5 rounded transition-colors"
              >
                <X size={12} />
              </button>
            </div>
          );
        })}

        <div className="flex-1" />

        {activeTab?.isDirty && (
          <button
            onClick={handleSave}
            className="flex items-center gap-1 px-2.5 py-1 mr-2 rounded bg-[#21262d] hover:bg-[#30363d] text-[#e6edf3] text-[11px] border border-[#30363d] transition-colors"
            title="Save File (Ctrl+S)"
          >
            <Save size={12} className="text-[#3fb950]" />
            <span>Save</span>
          </button>
        )}
      </div>

      {/* Monaco Editor Canvas */}
      <div className="flex-1 w-full h-full relative">
        <Editor
          height="100%"
          language={activeTab.language || 'plaintext'}
          value={activeTab.content}
          theme="vs-dark"
          options={{
            fontSize: 13,
            fontFamily: "'JetBrains Mono', monospace",
            minimap: { enabled: true },
            scrollBeyondLastLine: false,
            smoothScrolling: true,
            cursorBlinking: 'smooth',
            automaticLayout: true,
            tabSize: 4,
            renderLineHighlight: 'all',
          }}
          onChange={(val) => {
            if (val !== undefined) {
              updateFileContent(activeTab.path, val);
            }
          }}
        />
      </div>
    </div>
  );
};
