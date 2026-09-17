import React, { useEffect, useRef } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import { useAppStore } from '../../store/appStore';
import { createTerminalSession } from '../../api/client';
import { connectTerminalStream } from '../../api/ws';
import { RefreshCw, X, Plus } from 'lucide-react';

export const Terminal: React.FC = () => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const { 
    activeTerminalSessionId, 
    setActiveTerminalSessionId, 
    bottomPanelOpen, 
    setBottomPanelOpen 
  } = useAppStore();

  const initTerminal = async () => {
    if (!terminalRef.current) return;

    // Cleanup existing session if any
    if (wsRef.current) {
      wsRef.current.close();
    }
    if (xtermRef.current) {
      xtermRef.current.dispose();
    }

    const term = new XTerm({
      cursorBlink: true,
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 12,
      lineHeight: 1.2,
      theme: {
        background: '#0d1117',
        foreground: '#e6edf3',
        cursor: '#58a6ff',
        selectionBackground: '#30363d',
      },
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon());

    term.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = term;
    fitAddonRef.current = fitAddon;

    term.writeln('\x1b[38;2;88;166;255m🛡️ Bucker Agent Terminal\x1b[0m');
    term.writeln('\x1b[38;2;139;148;158mInitializing PTY session...\x1b[0m\r\n');

    try {
      const { session_id } = await createTerminalSession();
      setActiveTerminalSessionId(session_id);

      const ws = connectTerminalStream(session_id, (data) => {
        term.write(data);
      });

      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      });

      wsRef.current = ws;
    } catch (e) {
      term.writeln('\x1b[38;2;248;81;73mFailed to connect to backend PTY.\x1b[0m');
      console.error(e);
    }
  };

  useEffect(() => {
    if (bottomPanelOpen) {
      initTerminal();
    }
    return () => {
      wsRef.current?.close();
      xtermRef.current?.dispose();
    };
  }, [bottomPanelOpen]);

  useEffect(() => {
    const handleResize = () => {
      fitAddonRef.current?.fit();
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  if (!bottomPanelOpen) return null;

  return (
    <div className="flex flex-col h-56 bg-[#0d1117] border-t border-[#30363d]">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[#161b22] border-b border-[#30363d] text-xs">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-[#e6edf3]">TERMINAL</span>
          {activeTerminalSessionId && (
            <span className="text-[10px] font-mono text-[#8b949e]">
              [{activeTerminalSessionId.slice(0, 8)}]
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={initTerminal}
            className="p-1 hover:text-[#e6edf3] text-[#8b949e] transition-colors"
            title="Restart Terminal"
          >
            <RefreshCw size={12} />
          </button>
          <button
            onClick={() => setBottomPanelOpen(false)}
            className="p-1 hover:text-[#f85149] text-[#8b949e] transition-colors"
            title="Close Panel"
          >
            <X size={13} />
          </button>
        </div>
      </div>

      <div ref={terminalRef} className="flex-1 w-full overflow-hidden bg-[#0d1117]" />
    </div>
  );
};
