import {contextBridge, ipcRenderer} from 'electron';

export interface DesktopResponse<T> {ok: boolean; data?: T; error?: string;}

const bridge = {
  invoke: (command: string, payload?: unknown): Promise<DesktopResponse<any>> => ipcRenderer.invoke(`desktop:${command}`, payload),
  onEvent: (callback: (event: any) => void): (() => void) => {
    const listener = (_event: unknown, payload: any) => callback(payload);
    ipcRenderer.on('desktop:event', listener);
    return () => ipcRenderer.removeListener('desktop:event', listener);
  },
};
export type DesktopBridge = typeof bridge;
contextBridge.exposeInMainWorld('desktop', bridge);
