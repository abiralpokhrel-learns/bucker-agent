import { EventEmitter } from 'node:events';
import { ChildProcessWithoutNullStreams } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';

/** Newline-delimited ACP JSON-RPC. No terminal-output scraping. */
export class AcpClient extends EventEmitter {
  private seq = 0;
  private buffer = '';
  private decoder = new StringDecoder('utf8');
  private pending = new Map<number, {resolve: (v: any) => void; reject: (e: Error) => void; timer: NodeJS.Timeout}>();
  private permissions = new Map<string | number, any>();
  private closed = false;
  constructor(private child: ChildProcessWithoutNullStreams) {
    super();
    child.stdout.on('data', chunk => {
      this.buffer += this.decoder.write(chunk);
      if (this.buffer.length > 8 * 1024 * 1024) { this.close('ACP frame exceeds limit'); return; }
      let newline: number;
      while ((newline = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, newline); this.buffer = this.buffer.slice(newline + 1);
        if (!line.trim()) continue;
        try { this.receive(JSON.parse(line)); } catch { this.close('Invalid ACP JSON frame'); return; }
      }
    });
    child.on('error', () => this.close('Unable to start the agent. Select its executable in Settings.'));
    child.on('exit', code => this.close(`The agent stopped unexpectedly (${code ?? 'signal'}). Reconnect to continue.`));
    child.stdin.on('error', () => this.close('The agent stopped accepting input'));
    // Drain stderr without sending credentials or provider diagnostics to renderer.
    child.stderr.on('data', () => {});
  }
  private send(message: object) {
    if (this.closed) throw new Error('Agent connection is closed');
    this.child.stdin.write(JSON.stringify({jsonrpc: '2.0', ...message}) + '\n');
  }
  private receive(message: any) {
    if (message.method) {
      if (message.method === 'session/update') this.emit('update', message.params);
      else if (message.method === 'session/request_permission' && message.id !== undefined) {
        this.permissions.set(message.id, message.params);
        this.emit('permission', {id: message.id, ...message.params});
      } else if (message.id !== undefined) this.send({id: message.id, error: {code: -32601, message: 'Client method not supported'}});
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    clearTimeout(pending.timer); this.pending.delete(message.id);
    if (message.error) pending.reject(new Error(`Agent request failed (${message.error.code}): ${String(message.error.message).slice(0, 300)}`));
    else pending.resolve(message.result);
  }
  request(method: string, params: object, timeoutMs = 30000): Promise<any> {
    if (this.closed) return Promise.reject(new Error('Agent connection is closed'));
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`Hermes ${method} timed out`)); }, timeoutMs);
      this.pending.set(id, {resolve, reject, timer});
      try { this.send({id, method, params}); } catch (e) { clearTimeout(timer); this.pending.delete(id); reject(e); }
    });
  }
  notify(method: string, params: object) { this.send({method, params}); }
  respondPermission(id: string | number, optionId: string | null) {
    const permission = this.permissions.get(id);
    if (!permission) throw new Error('Permission request has expired');
    if (optionId !== null && !permission.options?.some((o: any) => o.optionId === optionId)) throw new Error('Invalid permission option');
    this.permissions.delete(id);
    this.send({id, result: {outcome: optionId === null ? {outcome: 'cancelled'} : {outcome: 'selected', optionId}}});
  }
  close(reason = 'Agent connection closed') {
    if (this.closed) return;
    this.closed = true;
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error(reason)); }
    this.pending.clear(); this.permissions.clear(); this.child.kill(); this.emit('closed', reason);
  }
}
