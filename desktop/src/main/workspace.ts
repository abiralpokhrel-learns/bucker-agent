import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import { createHash } from 'node:crypto';
const MAX_FILE = 2 * 1024 * 1024;
const excluded = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__']);
const hash = (data: Buffer | string) => createHash('sha256').update(data).digest('hex');
export class Workspace {
  private saving = new Set<string>();
  constructor(readonly root: string) {}
  private async resolve(relative: string): Promise<string> {
    if (typeof relative !== 'string' || path.isAbsolute(relative) || relative.includes('\0')) throw new Error('Path outside workspace');
    const segments = relative.split(/[\\/]/);
    if (segments.some(s => s === '.env' || s.startsWith('.env.') || s === '.git' || s === '.ssh' || /\.(pem|key)$/i.test(s))) throw new Error('Credential and repository metadata files are protected');
    const root = await fs.realpath(this.root);
    const target = path.resolve(root, relative);
    const relation = path.relative(root, target);
    if (relation.startsWith('..') || path.isAbsolute(relation)) throw new Error('Path outside workspace');
    // Reject symlinked components, including in-workspace links, for predictable writes.
    let cursor = root;
    for (const part of path.relative(root,target).split(path.sep).filter(Boolean)) {
      cursor = path.join(cursor, part);
      if ((await fs.lstat(cursor)).isSymbolicLink()) throw new Error('Symbolic links are not editable');
    }
    return target;
  }
  async tree(relative = '') {
    const target = await this.resolve(relative);
    const entries = await fs.readdir(target, {withFileTypes: true});
    return entries.filter(e => !excluded.has(e.name) && !e.name.startsWith('.env') && !e.isSymbolicLink())
      .sort((a,b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name))
      .slice(0, 2000).map(e => ({name: e.name, path: path.join(relative,e.name).replace(/\\/g,'/'), type: e.isDirectory() ? 'directory' : 'file'}));
  }
  async read(relative: string) {
    const target = await this.resolve(relative);
    const stat = await fs.stat(target);
    if (!stat.isFile() || stat.size > MAX_FILE) throw new Error('Only text files up to 2 MiB are supported');
    const data = await fs.readFile(target);
    if (data.includes(0)) throw new Error('Binary files are not editable');
    return {path: relative, content: new TextDecoder('utf-8', {fatal:true}).decode(data), version: hash(data)};
  }
  async save(relative: string, content: string, version: string) {
    if (typeof content !== 'string' || Buffer.byteLength(content) > MAX_FILE) throw new Error('File exceeds 2 MiB limit');
    const target = await this.resolve(relative);
    if (this.saving.has(target)) throw new Error('Save already in progress');
    this.saving.add(target);
    const temporary = target + '.bucker-' + process.pid + '.tmp';
    try {
      const before = await this.read(relative);
      if (before.version !== version) throw new Error('File changed on disk. Reload before saving.');
      const stat = await fs.stat(target);
      await fs.writeFile(temporary, content, {encoding: 'utf8', flag:'wx', mode: stat.mode});
      if ((await this.read(relative)).version !== version) throw new Error('File changed on disk. Reload before saving.');
      await fs.rename(temporary, target);
      return {version: hash(content)};
    } finally { await fs.rm(temporary,{force:true}); this.saving.delete(target); }
  }
}
