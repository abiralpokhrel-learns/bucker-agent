import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../store/appStore';
import { fetchFileTree, readFile } from '../../api/client';
import { FileNode } from '../../api/types';
import { 
  Folder, 
  FolderOpen, 
  FileCode, 
  FileText, 
  ChevronRight, 
  ChevronDown,
  RefreshCw
} from 'lucide-react';

interface FileItemProps {
  node: FileNode;
  level: number;
}

const FileItem: React.FC<FileItemProps> = ({ node, level }) => {
  const [isOpen, setIsOpen] = useState(level < 1);
  const { openFile } = useAppStore();

  const handleClick = async () => {
    if (node.type === 'directory') {
      setIsOpen(!isOpen);
    } else {
      try {
        const data = await readFile(node.path);
        openFile({
          path: node.path,
          name: node.name,
          content: data.content,
          language: data.language || 'plaintext',
        });
      } catch (err) {
        console.error('Failed to read file', err);
      }
    }
  };

  const isDir = node.type === 'directory';

  return (
    <div>
      <div
        onClick={handleClick}
        style={{ paddingLeft: `${level * 14 + 6}px` }}
        className="flex items-center gap-1.5 py-1 pr-2 hover:bg-[#21262d] cursor-pointer text-[#e6edf3] text-xs select-none transition-colors rounded"
      >
        {isDir ? (
          <>
            {isOpen ? <ChevronDown size={13} className="text-[#8b949e]" /> : <ChevronRight size={13} className="text-[#8b949e]" />}
            {isOpen ? <FolderOpen size={14} className="text-[#58a6ff]" /> : <Folder size={14} className="text-[#58a6ff]" />}
          </>
        ) : (
          <>
            <span className="w-3.5" />
            <FileCode size={14} className="text-[#39c5cf]" />
          </>
        )}
        <span className="truncate">{node.name}</span>
      </div>

      {isDir && isOpen && node.children && (
        <div>
          {node.children.map((child) => (
            <FileItem key={child.path} node={child} level={level + 1} />
          ))}
        </div>
      )}
    </div>
  );
};

export const FileTree: React.FC = () => {
  const { workspaceRoot } = useAppStore();
  const [tree, setTree] = useState<FileNode | null>(null);
  const [loading, setLoading] = useState(false);

  const loadTree = async () => {
    setLoading(true);
    try {
      const data = await fetchFileTree(workspaceRoot, 4);
      setTree(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTree();
  }, [workspaceRoot]);

  return (
    <div className="flex flex-col h-full bg-[#161b22] text-xs">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#30363d] text-[#8b949e] font-semibold uppercase tracking-wider text-[10px]">
        <span>Workspace Explorer</span>
        <button 
          onClick={loadTree} 
          title="Refresh tree" 
          className="hover:text-[#e6edf3] p-1 transition-colors"
        >
          <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto py-1">
        {tree ? (
          <FileItem node={tree} level={0} />
        ) : (
          <div className="p-4 text-center text-[#8b949e]">
            {loading ? 'Scanning workspace...' : 'No files found'}
          </div>
        )}
      </div>
    </div>
  );
};
