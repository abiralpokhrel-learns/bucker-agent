import { Task, TaskEvent, FileNode, DiffData, TerminalSessionInfo, ChatSession, ChatMessage } from './types';

const BASE_URL = '';

export async function fetchTasks(status?: string): Promise<Task[]> {
  const url = status ? `/tasks?status=${encodeURIComponent(status)}` : '/tasks';
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch tasks: ${res.statusText}`);
  return res.json();
}

export async function fetchTask(taskId: string): Promise<any> {
  const res = await fetch(`/tasks/${taskId}`);
  if (!res.ok) throw new Error(`Failed to fetch task: ${res.statusText}`);
  return res.json();
}

export async function fetchTaskEvents(taskId: string): Promise<TaskEvent[]> {
  const res = await fetch(`/tasks/${taskId}/events`);
  if (!res.ok) throw new Error(`Failed to fetch task events: ${res.statusText}`);
  return res.json();
}

export async function createTask(data: {
  objective: string;
  task_type?: string;
  verifier?: string;
  budget_usd?: number;
  deadline_minutes?: number;
  max_retries?: number;
}): Promise<{ task_id: string; workflow_id: string }> {
  const res = await fetch('/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Failed to create task: ${res.statusText}`);
  return res.json();
}

export async function cancelTask(taskId: string): Promise<void> {
  const res = await fetch(`/tasks/${taskId}/cancel`, { method: 'POST' });
  if (!res.ok) throw new Error(`Failed to cancel task: ${res.statusText}`);
}

export async function rerunTask(taskId: string): Promise<{ task_id: string }> {
  const res = await fetch(`/tasks/${taskId}/rerun`, { method: 'POST' });
  if (!res.ok) throw new Error(`Failed to rerun task: ${res.statusText}`);
  return res.json();
}

export async function approveTask(taskId: string, notes?: string): Promise<void> {
  const res = await fetch(`/tasks/${taskId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes }),
  });
  if (!res.ok) throw new Error(`Failed to approve task: ${res.statusText}`);
}

export async function rejectTask(taskId: string, notes?: string): Promise<void> {
  const res = await fetch(`/tasks/${taskId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes }),
  });
  if (!res.ok) throw new Error(`Failed to reject task: ${res.statusText}`);
}

// File System APIs
export async function fetchFileTree(root: string, maxDepth: number = 4): Promise<FileNode> {
  const res = await fetch(`/api/files/tree?root=${encodeURIComponent(root)}&max_depth=${maxDepth}`);
  if (!res.ok) throw new Error(`Failed to fetch file tree: ${res.statusText}`);
  return res.json();
}

export async function readFile(path: string): Promise<{ content: string; language: string; size: number }> {
  const res = await fetch(`/api/files/read?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`Failed to read file: ${res.statusText}`);
  return res.json();
}

export async function writeFile(path: string, content: string): Promise<{ backed_up: boolean; backup_ref?: string }> {
  const res = await fetch('/api/files/write', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  });
  if (!res.ok) throw new Error(`Failed to write file: ${res.statusText}`);
  return res.json();
}

export async function fetchDiff(taskId: string): Promise<DiffData> {
  const res = await fetch(`/api/files/diff?task_id=${encodeURIComponent(taskId)}`);
  if (!res.ok) throw new Error(`Failed to fetch diff: ${res.statusText}`);
  return res.json();
}

// Terminal APIs
export async function createTerminalSession(shell?: string): Promise<{ session_id: string; pid: number }> {
  const res = await fetch('/api/terminal/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shell }),
  });
  if (!res.ok) throw new Error(`Failed to create terminal session: ${res.statusText}`);
  return res.json();
}

export async function listTerminalSessions(): Promise<TerminalSessionInfo[]> {
  const res = await fetch('/api/terminal/sessions');
  if (!res.ok) throw new Error(`Failed to list terminal sessions: ${res.statusText}`);
  return res.json();
}

export async function closeTerminalSession(sessionId: string): Promise<void> {
  const res = await fetch(`/api/terminal/sessions/${sessionId}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`Failed to close terminal session: ${res.statusText}`);
}

// Chat APIs
export async function fetchChatSessions(): Promise<ChatSession[]> {
  const res = await fetch('/api/chat/sessions');
  if (!res.ok) throw new Error(`Failed to list chat sessions: ${res.statusText}`);
  return res.json();
}

export async function createChatSession(title?: string, projectRoot?: string): Promise<ChatSession> {
  const res = await fetch('/api/chat/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, project_root: projectRoot }),
  });
  if (!res.ok) throw new Error(`Failed to create chat session: ${res.statusText}`);
  return res.json();
}

export async function fetchChatSession(sessionId: string): Promise<ChatSession> {
  const res = await fetch(`/api/chat/sessions/${sessionId}`);
  if (!res.ok) throw new Error(`Failed to fetch chat session: ${res.statusText}`);
  return res.json();
}

export async function sendChatMessage(sessionId: string, content: string, context?: any): Promise<{ message_id: string }> {
  const res = await fetch(`/api/chat/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, context }),
  });
  if (!res.ok) throw new Error(`Failed to send chat message: ${res.statusText}`);
  return res.json();
}

export async function applyChatSuggestion(sessionId: string, messageId: string, objective?: string): Promise<any> {
  const res = await fetch(`/api/chat/sessions/${sessionId}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: messageId, objective }),
  });
  if (!res.ok) throw new Error(`Failed to apply suggestion: ${res.statusText}`);
  return res.json();
}

// System & Telemetry
export async function fetchSystemStatus(): Promise<any> {
  const res = await fetch('/api/system');
  if (!res.ok) throw new Error(`Failed to fetch system status: ${res.statusText}`);
  return res.json();
}

export async function fetchUsage(): Promise<any> {
  const res = await fetch('/api/usage');
  if (!res.ok) throw new Error(`Failed to fetch usage: ${res.statusText}`);
  return res.json();
}

export async function fetchModels(): Promise<any> {
  const res = await fetch('/api/models');
  if (!res.ok) throw new Error(`Failed to fetch models: ${res.statusText}`);
  return res.json();
}
