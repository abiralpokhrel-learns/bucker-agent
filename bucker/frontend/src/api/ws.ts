export function getWsUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  return `${protocol}//${host}${path}`;
}

export function connectTaskStream(
  taskId: string,
  onEvent: (event: any) => void,
  onTerminal?: (status: string) => void
): WebSocket {
  const ws = new WebSocket(getWsUrl(`/ws/tasks/${taskId}`));

  ws.onmessage = (msg) => {
    try {
      const data = JSON.parse(msg.data);
      if (data.type === 'event') {
        onEvent(data.event);
      } else if (data.type === 'terminal' && onTerminal) {
        onTerminal(data.status);
      }
    } catch (e) {
      console.error('Error parsing task WS message', e);
    }
  };

  return ws;
}

export function connectAgentStream(
  sessionId: string,
  onMessage: (msg: any) => void
): WebSocket {
  const ws = new WebSocket(getWsUrl(`/ws/agent/${sessionId}`));

  ws.onmessage = (msg) => {
    try {
      const data = JSON.parse(msg.data);
      onMessage(data);
    } catch (e) {
      console.error('Error parsing agent WS message', e);
    }
  };

  return ws;
}

export function connectTerminalStream(
  sessionId: string,
  onData: (data: string) => void
): WebSocket {
  const ws = new WebSocket(getWsUrl(`/ws/terminal/${sessionId}`));

  ws.onmessage = (msg) => {
    onData(msg.data);
  };

  return ws;
}
