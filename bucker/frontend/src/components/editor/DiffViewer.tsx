import React, { useEffect, useState } from 'react';
import { DiffEditor } from '@monaco-editor/react';
import { useAppStore } from '../../store/appStore';
import { fetchDiff, approveTask, rejectTask, rerunTask } from '../../api/client';
import { DiffData } from '../../api/types';
import { Check, X, RotateCcw, AlertTriangle, FileText } from 'lucide-react';

export const DiffViewer: React.FC = () => {
  const { activeDiffTaskId, setActiveDiffTaskId } = useAppStore();
  const [diffData, setDiffData] = useState<DiffData | null>(null);
  const [activeFileIndex, setActiveFileIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!activeDiffTaskId) return;
    setLoading(true);
    fetchDiff(activeDiffTaskId)
      .then(setDiffData)
      .catch((e) => console.error('Failed to load diff', e))
      .finally(() => setLoading(false));
  }, [activeDiffTaskId]);

  if (!activeDiffTaskId) return null;

  const currentFile = diffData?.files?.[activeFileIndex];

  const handleApprove = async () => {
    try {
      await approveTask(activeDiffTaskId);
      setActionMessage('Task changes approved and committed!');
      setTimeout(() => setActiveDiffTaskId(null), 1500);
    } catch (e) {
      console.error(e);
    }
  };

  const handleReject = async () => {
    try {
      await rejectTask(activeDiffTaskId);
      setActionMessage('Task changes rejected.');
      setTimeout(() => setActiveDiffTaskId(null), 1500);
    } catch (e) {
      console.error(e);
    }
  };

  const handleRerun = async () => {
    try {
      const res = await rerunTask(activeDiffTaskId);
      setActionMessage(`Task restarted as ${res.task_id.slice(0, 8)}`);
      setTimeout(() => setActiveDiffTaskId(res.task_id), 1500);
    } catch (e) {
      console.error(e);
    }
  };

  return (
    <div className="flex flex-col h-full bg-[#0d1117]">
      {/* Header Banner */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-[#161b22] border-b border-[#30363d] text-xs">
        <div className="flex items-center gap-3">
          <span className="font-bold text-[#e6edf3]">Diff Inspection:</span>
          <span className="font-mono text-[#58a6ff]">{activeDiffTaskId.slice(0, 10)}</span>
          {diffData?.files && (
            <span className="text-[#8b949e]">
              ({diffData.files.length} file{diffData.files.length !== 1 ? 's' : ''} modified)
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {actionMessage ? (
            <span className="text-[#3fb950] font-medium">{actionMessage}</span>
          ) : (
            <>
              <button
                onClick={handleRerun}
                className="flex items-center gap-1.5 px-3 py-1 rounded bg-[#21262d] hover:bg-[#30363d] text-[#e6edf3] border border-[#30363d] transition-colors"
              >
                <RotateCcw size={12} className="text-[#58a6ff]" />
                <span>Re-run</span>
              </button>
              <button
                onClick={handleReject}
                className="flex items-center gap-1.5 px-3 py-1 rounded bg-[#b62324] hover:bg-[#d03535] text-white font-medium transition-colors"
              >
                <X size={12} />
                <span>Reject</span>
              </button>
              <button
                onClick={handleApprove}
                className="flex items-center gap-1.5 px-3 py-1 rounded bg-[#238636] hover:bg-[#2ea043] text-white font-medium transition-colors"
              >
                <Check size={12} />
                <span>Accept & Commit</span>
              </button>
              <button
                onClick={() => setActiveDiffTaskId(null)}
                className="p-1 hover:text-[#f85149] text-[#8b949e] ml-2"
                title="Close Diff"
              >
                <X size={15} />
              </button>
            </>
          )}
        </div>
      </div>

      {/* Multiple Files Picker if > 1 */}
      {diffData?.files && diffData.files.length > 1 && (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-[#1c2333] border-b border-[#30363d] overflow-x-auto text-xs">
          {diffData.files.map((file, idx) => (
            <button
              key={file.path}
              onClick={() => setActiveFileIndex(idx)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded transition-colors ${
                idx === activeFileIndex
                  ? 'bg-[#21262d] text-[#58a6ff] border border-[#30363d]'
                  : 'text-[#8b949e] hover:text-[#e6edf3]'
              }`}
            >
              <FileText size={12} />
              <span>{file.path}</span>
            </button>
          ))}
        </div>
      )}

      {/* Monaco Diff Editor */}
      <div className="flex-1 w-full h-full relative">
        {loading ? (
          <div className="flex items-center justify-center h-full text-[#8b949e] text-xs">
            Loading diff payload...
          </div>
        ) : currentFile ? (
          <DiffEditor
            height="100%"
            original={currentFile.original || ''}
            modified={currentFile.modified || ''}
            language={currentFile.language || 'python'}
            theme="vs-dark"
            options={{
              fontSize: 13,
              fontFamily: "'JetBrains Mono', monospace",
              renderSideBySide: true,
              readOnly: true,
              automaticLayout: true,
            }}
          />
        ) : (
          <div className="flex items-center justify-center h-full text-[#8b949e] text-xs">
            No diff files recorded for this task.
          </div>
        )}
      </div>
    </div>
  );
};
