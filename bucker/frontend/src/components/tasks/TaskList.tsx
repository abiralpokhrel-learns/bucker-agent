import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../store/appStore';
import { fetchTasks, createTask } from '../../api/client';
import { Task } from '../../api/types';
import { 
  CheckCircle, 
  AlertCircle, 
  Clock, 
  Play, 
  Plus, 
  RefreshCw, 
  DollarSign, 
  Code,
  ShieldCheck
} from 'lucide-react';

export const TaskList: React.FC = () => {
  const { 
    tasks, 
    setTasks, 
    selectedTaskId, 
    setSelectedTaskId, 
    setActiveDiffTaskId 
  } = useAppStore();

  const [filter, setFilter] = useState<string>('all');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newObjective, setNewObjective] = useState('');
  const [newVerifier, setNewVerifier] = useState('python_test_runner');
  const [newBudget, setNewBudget] = useState(0.75);
  const [loading, setLoading] = useState(false);

  const loadTasks = async () => {
    setLoading(true);
    try {
      const data = await fetchTasks(filter === 'all' ? undefined : filter);
      setTasks(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTasks();
    const interval = setInterval(loadTasks, 4000);
    return () => clearInterval(interval);
  }, [filter]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newObjective.trim()) return;
    try {
      const res = await createTask({
        objective: newObjective,
        verifier: newVerifier,
        budget_usd: newBudget,
      });
      setShowCreateModal(false);
      setNewObjective('');
      setSelectedTaskId(res.task_id);
      loadTasks();
    } catch (err) {
      console.error(err);
    }
  };

  const getStatusBadge = (status: Task['status']) => {
    switch (status) {
      case 'completed':
        return (
          <span className="flex items-center gap-1 text-[#3fb950] font-semibold">
            <CheckCircle size={11} /> PASSED
          </span>
        );
      case 'failed':
      case 'verification_failed':
        return (
          <span className="flex items-center gap-1 text-[#f85149] font-semibold">
            <AlertCircle size={11} /> FAILED
          </span>
        );
      case 'needs_human_review':
        return (
          <span className="flex items-center gap-1 text-[#bc8cff] font-semibold">
            <AlertCircle size={11} /> REVIEW
          </span>
        );
      case 'in_progress':
        return (
          <span className="flex items-center gap-1 text-[#58a6ff] font-semibold animate-pulse">
            <Play size={11} /> RUNNING
          </span>
        );
      default:
        return (
          <span className="flex items-center gap-1 text-[#8b949e]">
            <Clock size={11} /> {status}
          </span>
        );
    }
  };

  return (
    <div className="flex flex-col h-full bg-[#161b22] text-xs">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#30363d]">
        <div className="flex items-center gap-1 font-semibold text-[#e6edf3]">
          <ShieldCheck size={14} className="text-[#3fb950]" />
          <span>VERIFIED TASKS</span>
        </div>

        <div className="flex items-center gap-1">
          <button
            onClick={() => setShowCreateModal(true)}
            className="flex items-center gap-1 px-2 py-0.5 rounded bg-[#238636] hover:bg-[#2ea043] text-white font-medium"
            title="Create Task"
          >
            <Plus size={12} />
            <span>New</span>
          </button>
          <button
            onClick={loadTasks}
            className="p-1 hover:text-[#e6edf3] text-[#8b949e]"
            title="Refresh"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Filter Chips */}
      <div className="flex gap-1 px-3 py-1.5 border-b border-[#30363d] overflow-x-auto text-[11px]">
        {['all', 'in_progress', 'completed', 'failed', 'needs_human_review'].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-0.5 rounded-full capitalize transition-colors ${
              filter === f
                ? 'bg-[#30363d] text-[#e6edf3] font-medium'
                : 'text-[#8b949e] hover:text-[#e6edf3]'
            }`}
          >
            {f.replace('_', ' ')}
          </button>
        ))}
      </div>

      {/* Task List items */}
      <div className="flex-1 overflow-y-auto divide-y divide-[#21262d]">
        {tasks.map((t) => {
          const isSelected = t.task_id === selectedTaskId;
          return (
            <div
              key={t.task_id}
              onClick={() => {
                setSelectedTaskId(t.task_id);
                setActiveDiffTaskId(t.task_id);
              }}
              className={`p-3 cursor-pointer select-none transition-colors ${
                isSelected ? 'bg-[#21262d] border-l-2 border-l-[#58a6ff]' : 'hover:bg-[#1c2333]'
              }`}
            >
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-mono text-[#58a6ff] text-[11px]">
                  {t.task_id.slice(0, 8)}
                </span>
                {getStatusBadge(t.status)}
              </div>

              <div className="text-[#e6edf3] line-clamp-2 leading-relaxed mb-2 font-normal">
                {t.objective}
              </div>

              <div className="flex items-center justify-between text-[10px] text-[#8b949e]">
                <div className="flex items-center gap-2">
                  <span className="flex items-center gap-0.5">
                    <DollarSign size={10} className="text-[#3fb950]" />
                    ${(t.cost_usd || 0).toFixed(4)}
                  </span>
                  <span>{t.tokens || 0} tokens</span>
                </div>
                <span className="font-mono text-[10px] text-[#8b949e]">
                  {t.verifier || 'python'}
                </span>
              </div>
            </div>
          );
        })}

        {tasks.length === 0 && (
          <div className="p-6 text-center text-[#8b949e]">
            {loading ? 'Fetching tasks...' : 'No tasks found.'}
          </div>
        )}
      </div>

      {/* Create Task Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
          <div className="bg-[#161b22] border border-[#30363d] rounded-lg w-full max-w-md p-4 text-xs">
            <h3 className="text-sm font-bold text-[#e6edf3] mb-3 flex items-center gap-1.5">
              <Plus size={15} className="text-[#3fb950]" /> Create Verified Task
            </h3>

            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="block text-[#8b949e] mb-1 font-medium">Objective</label>
                <textarea
                  value={newObjective}
                  onChange={(e) => setNewObjective(e.target.value)}
                  placeholder="e.g. Fix broken edge cases in auth token verification..."
                  rows={3}
                  required
                  className="w-full bg-[#0d1117] border border-[#30363d] rounded p-2 text-[#e6edf3] outline-none focus:border-[#58a6ff]"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[#8b949e] mb-1 font-medium">Verifier Engine</label>
                  <select
                    value={newVerifier}
                    onChange={(e) => setNewVerifier(e.target.value)}
                    className="w-full bg-[#0d1117] border border-[#30363d] rounded p-1.5 text-[#e6edf3] outline-none"
                  >
                    <option value="python_test_runner">pytest (Python)</option>
                    <option value="typescript_verifier">tsc + jest/vitest</option>
                    <option value="rust_verifier">cargo test (Rust)</option>
                    <option value="go_verifier">go test (Go)</option>
                    <option value="lint_verifier">Linter (Ruff/ESLint)</option>
                    <option value="noop">noop (Skip sandbox)</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[#8b949e] mb-1 font-medium">Budget USD ($)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={newBudget}
                    onChange={(e) => setNewBudget(parseFloat(e.target.value))}
                    className="w-full bg-[#0d1117] border border-[#30363d] rounded p-1.5 text-[#e6edf3] outline-none"
                  />
                </div>
              </div>

              <div className="flex justify-end gap-2 pt-2 border-t border-[#30363d]">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="px-3 py-1.5 rounded bg-[#21262d] hover:bg-[#30363d] text-[#8b949e] hover:text-[#e6edf3]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1.5 rounded bg-[#238636] hover:bg-[#2ea043] text-white font-medium"
                >
                  Start Verified Task
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
