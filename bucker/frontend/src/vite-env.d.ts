/// <reference types="vite/client" />

declare module 'monaco-editor/esm/vs/editor/editor.worker?worker&inline' {
  const workerConstructor: new () => Worker;
  export default workerConstructor;
}
