export interface Task {
  task_id: string;
  task_type: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'verification_failed' | 'halted' | 'needs_human_review';
  objective: string;
  cost_usd: number;
  tokens: number;
  events_count?: number;
  created_at?: string;
  verifier?: string;
  budget_usd?: number;
  deadline_minutes?: number;
}

export interface TaskEvent {
  id: number;
  task_id: string;
  event_type: string;
  payload: Record<string, any>;
  created_at: string;
}

export interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number | null;
  language?: string | null;
  children?: FileNode[];
}

export interface DiffFile {
  path: string;
  original: string;
  modified: string;
  language?: string;
}

export interface DiffData {
  files: DiffFile[];
}

export interface TerminalSessionInfo {
  session_id: string;
  pid: number;
  alive: boolean;
  created_at: string;
  shell: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  metadata?: Record<string, any>;
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: string;
  project_root?: string | null;
  messages: ChatMessage[];
}

export interface SystemStatus {
  primary_model?: string;
  fallbacks?: string[];
  mode?: string;
  postgres_ok?: boolean;
  temporal_ok?: boolean;
  docker_ok?: boolean;
  tasks_count?: number;
}
