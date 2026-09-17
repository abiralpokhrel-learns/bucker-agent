import { create } from 'zustand';
import { Task, FileNode, DiffData } from '../api/types';

export interface EditorTab {
  path: string;
  name: string;
  content: string;
  language: string;
  isDirty?: boolean;
}

interface AppState {
  // Navigation & Active Layout
  activeSidebarTab: 'files' | 'tasks' | 'chat' | 'dashboard';
  setActiveSidebarTab: (tab: 'files' | 'tasks' | 'chat' | 'dashboard') => void;
  
  workspaceRoot: string;
  setWorkspaceRoot: (root: string) => void;

  // Editor Tabs
  openTabs: EditorTab[];
  activeTabPath: string | null;
  openFile: (file: EditorTab) => void;
  closeTab: (path: string) => void;
  setActiveTabPath: (path: string) => void;
  updateFileContent: (path: string, content: string) => void;

  // Diff Viewer
  activeDiffTaskId: string | null;
  activeDiffData: DiffData | null;
  setActiveDiffTaskId: (taskId: string | null) => void;
  setActiveDiffData: (diff: DiffData | null) => void;

  // Bottom Panel (Terminal)
  bottomPanelOpen: boolean;
  toggleBottomPanel: () => void;
  setBottomPanelOpen: (open: boolean) => void;
  activeTerminalSessionId: string | null;
  setActiveTerminalSessionId: (id: string | null) => void;

  // Right Panel (AI Chat Dock)
  rightPanelOpen: boolean;
  toggleRightPanel: () => void;
  setRightPanelOpen: (open: boolean) => void;
  activeChatSessionId: string | null;
  setActiveChatSessionId: (id: string | null) => void;

  // Tasks
  tasks: Task[];
  setTasks: (tasks: Task[]) => void;
  selectedTaskId: string | null;
  setSelectedTaskId: (id: string | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  activeSidebarTab: 'files',
  setActiveSidebarTab: (tab) => set({ activeSidebarTab: tab }),

  workspaceRoot: 'c:/abiralprojects/bucker agent',
  setWorkspaceRoot: (root) => set({ workspaceRoot: root }),

  openTabs: [],
  activeTabPath: null,
  openFile: (file) =>
    set((state) => {
      const exists = state.openTabs.some((t) => t.path === file.path);
      return {
        openTabs: exists ? state.openTabs : [...state.openTabs, file],
        activeTabPath: file.path,
      };
    }),
  closeTab: (path) =>
    set((state) => {
      const nextTabs = state.openTabs.filter((t) => t.path !== path);
      const nextActive = state.activeTabPath === path ? (nextTabs[nextTabs.length - 1]?.path ?? null) : state.activeTabPath;
      return { openTabs: nextTabs, activeTabPath: nextActive };
    }),
  setActiveTabPath: (path) => set({ activeTabPath: path }),
  updateFileContent: (path, content) =>
    set((state) => ({
      openTabs: state.openTabs.map((t) =>
        t.path === path ? { ...t, content, isDirty: true } : t
      ),
    })),

  activeDiffTaskId: null,
  activeDiffData: null,
  setActiveDiffTaskId: (taskId) => set({ activeDiffTaskId: taskId }),
  setActiveDiffData: (diff) => set({ activeDiffData: diff }),

  bottomPanelOpen: true,
  toggleBottomPanel: () => set((state) => ({ bottomPanelOpen: !state.bottomPanelOpen })),
  setBottomPanelOpen: (open) => set({ bottomPanelOpen: open }),
  activeTerminalSessionId: null,
  setActiveTerminalSessionId: (id) => set({ activeTerminalSessionId: id }),

  rightPanelOpen: true,
  toggleRightPanel: () => set((state) => ({ rightPanelOpen: !state.rightPanelOpen })),
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
  activeChatSessionId: null,
  setActiveChatSessionId: (id) => set({ activeChatSessionId: id }),

  tasks: [],
  setTasks: (tasks) => set({ tasks }),
  selectedTaskId: null,
  setSelectedTaskId: (id) => set({ selectedTaskId: id }),
}));
