export interface DesktopResponse<T> {ok: boolean; data?: T; error?: string;}
export interface DesktopBridge {
  invoke: (command: string, payload?: unknown) => Promise<DesktopResponse<unknown>>;
  onEvent: (callback: (event: DesktopEvent) => void) => () => void;
}
export interface DesktopEvent {type: 'update' | 'permission' | 'status' | 'error'; [key: string]: any;}

declare global {interface Window {desktop: DesktopBridge;}}

export const bridge: DesktopBridge = (typeof window !== 'undefined' && window.desktop) || {
  async invoke() { return {ok: false, error: 'Desktop bridge unavailable (running outside Electron)'}; },
  onEvent() { return () => {}; },
};

export async function call<T>(command: string, payload?: unknown): Promise<T> {
  const response = await bridge.invoke(command, payload);
  if (!response.ok) throw new Error(response.error ?? 'Unknown desktop error');
  return response.data as T;
}

export type ProviderInfo = {id: string; name: string; signup: string; docs: string; tier: string; notes: string; steps: string[]; connected: boolean; models: {id: string; context: number; maxOutput: number}[]};
export type Status = {workspace: string | null; sessionId: string | null; busy: boolean; connected: boolean; hermesPath: string | null; connections: {provider: string; model: string}[]; events: any[]};
export type FileEntry = {name: string; path: string; type: 'directory' | 'file'};
export type AgentUpdate = {sessionId?: string; update?: any};
export type PermissionRequest = {id: number | string; toolCall?: any; options: {optionId: string; name: string; kind: string}[]};
