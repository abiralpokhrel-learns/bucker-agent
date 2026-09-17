import React, { useEffect, useState, useRef } from 'react';
import { useAppStore } from '../../store/appStore';
import { 
  fetchChatSessions, 
  createChatSession, 
  fetchChatSession, 
  sendChatMessage,
  applyChatSuggestion
} from '../../api/client';
import { connectAgentStream } from '../../api/ws';
import { ChatSession, ChatMessage } from '../../api/types';
import { 
  Bot, 
  Send, 
  Plus, 
  Code2, 
  CheckCircle2, 
  X, 
  Sparkles,
  ArrowUpRight
} from 'lucide-react';

export const ChatPanel: React.FC = () => {
  const { 
    rightPanelOpen, 
    setRightPanelOpen, 
    activeChatSessionId, 
    setActiveChatSessionId,
    workspaceRoot,
    setSelectedTaskId,
    setActiveSidebarTab
  } = useAppStore();

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [currentSession, setCurrentSession] = useState<ChatSession | null>(null);
  const [input, setInput] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const loadSessions = async () => {
    try {
      const list = await fetchChatSessions();
      setSessions(list);
      if (list.length > 0 && !activeChatSessionId) {
        setActiveChatSessionId(list[0].id);
      } else if (list.length === 0) {
        handleNewSession();
      }
    } catch (e) {
      console.error(e);
    }
  };

  const loadCurrentSession = async (id: string) => {
    try {
      const data = await fetchChatSession(id);
      setCurrentSession(data);
    } catch (e) {
      console.error(e);
    }
  };

  const handleNewSession = async () => {
    try {
      const newSession = await createChatSession('New Chat', workspaceRoot);
      setSessions([newSession, ...sessions]);
      setActiveChatSessionId(newSession.id);
      setCurrentSession(newSession);
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    loadSessions();
  }, []);

  useEffect(() => {
    if (activeChatSessionId) {
      loadCurrentSession(activeChatSessionId);
    }
  }, [activeChatSessionId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [currentSession?.messages]);

  const handleSend = async () => {
    if (!input.trim() || !activeChatSessionId || isGenerating) return;

    const userText = input;
    setInput('');

    // Optimistic UI update
    const userMsg: ChatMessage = {
      id: Math.random().toString(),
      role: 'user',
      content: userText,
      timestamp: new Date().toISOString(),
    };

    setCurrentSession((prev) =>
      prev ? { ...prev, messages: [...prev.messages, userMsg] } : null
    );

    setIsGenerating(true);

    try {
      await sendChatMessage(activeChatSessionId, userText);
      // Connect agent ws to stream tokens
      const ws = connectAgentStream(activeChatSessionId, (event) => {
        if (event.type === 'thought') {
          // Append or update response
        } else if (event.type === 'done' || event.type === 'error') {
          setIsGenerating(false);
          loadCurrentSession(activeChatSessionId);
          ws.close();
        }
      });
      // Fallback reload after 2s if WS already closed
      setTimeout(() => {
        loadCurrentSession(activeChatSessionId);
        setIsGenerating(false);
      }, 2500);
    } catch (e) {
      console.error(e);
      setIsGenerating(false);
    }
  };

  const handleConvertToTask = async (msgId: string, content: string) => {
    if (!activeChatSessionId) return;
    try {
      const res = await applyChatSuggestion(activeChatSessionId, msgId, content);
      if (res?.task_id) {
        setSelectedTaskId(res.task_id);
        setActiveSidebarTab('tasks');
      }
    } catch (e) {
      console.error(e);
    }
  };

  if (!rightPanelOpen) return null;

  return (
    <div className="w-80 h-full bg-[#161b22] border-l border-[#30363d] flex flex-col text-xs">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#30363d]">
        <div className="flex items-center gap-2">
          <Bot size={15} className="text-[#bc8cff]" />
          <span className="font-bold text-[#e6edf3]">Agent Cockpit</span>
        </div>

        <div className="flex items-center gap-1">
          <button
            onClick={handleNewSession}
            title="New Chat Session"
            className="p-1 hover:bg-[#21262d] rounded text-[#8b949e] hover:text-[#e6edf3] transition-colors"
          >
            <Plus size={14} />
          </button>
          <button
            onClick={() => setRightPanelOpen(false)}
            className="p-1 hover:bg-[#21262d] rounded text-[#8b949e] hover:text-[#f85149] transition-colors"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      {/* Message List */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {currentSession?.messages && currentSession.messages.length > 0 ? (
          currentSession.messages.map((msg) => {
            const isUser = msg.role === 'user';
            const hasCode = msg.content.includes('```');

            return (
              <div
                key={msg.id}
                className={`flex flex-col ${isUser ? 'items-end' : 'items-start'}`}
              >
                <div
                  className={`max-w-[92%] px-3 py-2 rounded-lg leading-relaxed ${
                    isUser
                      ? 'bg-[#1f6feb] text-white'
                      : 'bg-[#21262d] text-[#e6edf3] border border-[#30363d]'
                  }`}
                >
                  <div className="whitespace-pre-wrap break-words">{msg.content}</div>

                  {!isUser && (
                    <div className="mt-2 pt-2 border-t border-[#30363d] flex items-center justify-between text-[11px] text-[#8b949e]">
                      <span className="flex items-center gap-1 text-[#3fb950]">
                        <Sparkles size={11} /> Verified Logic
                      </span>
                      <button
                        onClick={() => handleConvertToTask(msg.id, msg.content)}
                        className="flex items-center gap-1 text-[#58a6ff] hover:underline"
                        title="Create a verified Bucker Task from this suggestion"
                      >
                        <span>Run as Task</span>
                        <ArrowUpRight size={12} />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center text-[#8b949e] p-4">
            <Bot size={28} className="text-[#bc8cff] mb-2" />
            <p className="font-medium text-[#e6edf3]">How can I help with the codebase?</p>
            <p className="text-[11px] mt-1">
              Ask questions, propose refactors, or ask me to implement features with guaranteed verification.
            </p>
          </div>
        )}

        {isGenerating && (
          <div className="flex items-center gap-2 text-[#8b949e] text-[11px] italic">
            <span className="w-2 h-2 rounded-full bg-[#bc8cff] animate-pulse" />
            <span>Agent reasoning and generating code...</span>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Box */}
      <div className="p-2 border-t border-[#30363d] bg-[#0d1117]">
        <div className="flex items-center gap-2 bg-[#161b22] border border-[#30363d] rounded-md px-2 py-1.5 focus-within:border-[#58a6ff] transition-colors">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask agent or type instruction..."
            rows={2}
            className="flex-1 bg-transparent text-[#e6edf3] text-xs resize-none outline-none placeholder:text-[#8b949e]"
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || isGenerating}
            className="p-1.5 rounded bg-[#238636] hover:bg-[#2ea043] disabled:opacity-40 text-white transition-colors"
          >
            <Send size={13} />
          </button>
        </div>
      </div>
    </div>
  );
};
