import React from 'react';
import { useAppStore } from '../../store/appStore';
import { FileTree } from '../editor/FileTree';
import { TaskList } from '../tasks/TaskList';
import { 
  Files, 
  ListTodo, 
  Bot, 
  BarChart2, 
  Settings, 
  ChevronLeft,
  ChevronRight 
} from 'lucide-react';

export const Sidebar: React.FC = () => {
  const { 
    activeSidebarTab, 
    setActiveSidebarTab, 
    toggleRightPanel, 
    rightPanelOpen 
  } = useAppStore();

  const [expanded, setExpanded] = React.useState(true);

  return (
    <div className="flex h-full border-r border-[#30363d] bg-[#161b22]">
      {/* Activity Bar (Icons) */}
      <div className="w-11 bg-[#0d1117] border-r border-[#30363d] flex flex-col items-center py-2 gap-3 text-[#8b949e]">
        <button
          onClick={() => {
            setActiveSidebarTab('files');
            setExpanded(true);
          }}
          title="Explorer"
          className={`p-2 rounded-md hover:text-[#e6edf3] transition-colors ${
            activeSidebarTab === 'files' && expanded
              ? 'text-[#58a6ff] bg-[#21262d] border-l-2 border-l-[#58a6ff]'
              : ''
          }`}
        >
          <Files size={18} />
        </button>

        <button
          onClick={() => {
            setActiveSidebarTab('tasks');
            setExpanded(true);
          }}
          title="Tasks"
          className={`p-2 rounded-md hover:text-[#e6edf3] transition-colors ${
            activeSidebarTab === 'tasks' && expanded
              ? 'text-[#58a6ff] bg-[#21262d] border-l-2 border-l-[#58a6ff]'
              : ''
          }`}
        >
          <ListTodo size={18} />
        </button>

        <button
          onClick={toggleRightPanel}
          title="Toggle Agent Chat"
          className={`p-2 rounded-md hover:text-[#e6edf3] transition-colors ${
            rightPanelOpen ? 'text-[#bc8cff]' : ''
          }`}
        >
          <Bot size={18} />
        </button>

        <div className="flex-1" />

        <button
          onClick={() => setExpanded(!expanded)}
          title="Toggle Sidebar"
          className="p-2 hover:text-[#e6edf3] transition-colors"
        >
          {expanded ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
        </button>
      </div>

      {/* Primary Drawer */}
      {expanded && (
        <div className="w-64 h-full bg-[#161b22] flex flex-col overflow-hidden">
          {activeSidebarTab === 'files' && <FileTree />}
          {activeSidebarTab === 'tasks' && <TaskList />}
        </div>
      )}
    </div>
  );
};
