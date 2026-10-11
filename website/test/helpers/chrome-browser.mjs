import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CHROME_NO_SANDBOX_ENV = 'ARCHIFY_CHROME_NO_SANDBOX';
const CHROME_STARTUP_TIMEOUT_MS = 90000;

function executable(file, platform = process.platform) {
  if (!file) return null;
  try {
    fs.accessSync(file, platform === 'win32' ? fs.constants.F_OK : fs.constants.X_OK);
    return path.resolve(file);
  } catch {
    return null;
  }
}

function findOnPath(command, env, platform, resolveExecutable) {
  const pathApi = platform === 'win32' ? path.win32 : path;
  const directories = String(env.PATH || '').split(pathApi.delimiter).filter(Boolean);
  const extensions = platform === 'win32'
    ? String(env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean)
    : [''];
  for (const directory of directories) {
    for (const extension of extensions) {
      const candidate = pathApi.join(directory, `${command}${extension}`);
      const resolved = resolveExecutable(candidate, platform);
      if (resolved) return resolved;
    }
  }
  return null;
}

export function findChrome({
  env = process.env,
  platform = process.platform,
  resolveExecutable = executable,
} = {}) {
  if (Object.prototype.hasOwnProperty.call(env, 'ARCHIFY_CHROME')) {
    return resolveExecutable(env.ARCHIFY_CHROME, platform);
  }

  const fixed = [];
  const commands = [];
  if (platform === 'darwin') {
    fixed.push(
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
    );
  } else if (platform === 'win32') {
    const pathApi = path.win32;
    for (const root of [env.PROGRAMFILES, env['PROGRAMFILES(X86)'], env.LOCALAPPDATA].filter(Boolean)) {
      fixed.push(
        pathApi.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        pathApi.join(root, 'Chromium', 'Application', 'chrome.exe'),
      );
    }
    commands.push('chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser');
  } else {
    commands.push('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser');
  }

  for (const candidate of fixed) {
    const resolved = resolveExecutable(candidate, platform);
    if (resolved) return resolved;
  }
  for (const command of commands) {
    const resolved = findOnPath(command, env, platform, resolveExecutable);
    if (resolved) return resolved;
  }
  return null;
}

export class PipeCdp {
  constructor(child, { failureDetails = () => '' } = {}) {
    this.child = child;
    this.failureDetails = failureDetails;
    this.nextId = 1;
    this.buffer = '';
    this.receivedBytes = 0;
    this.receivedMessages = 0;
    this.completedWrites = 0;
    this.writtenBytes = 0;
    this.pending = new Map();
    this.waiters = [];
    this.writePipe = child.stdio[3];
    this.readPipe = child.stdio[4];
    this.readPipe.setEncoding('utf8');
    this.readPipe.on('data', (chunk) => this.consume(chunk));
    this.writePipe.on('error', (error) => this.failAll(this.failure('write pipe', error)));
    this.readPipe.on('error', (error) => this.failAll(this.failure('read pipe', error)));
    this.readPipe.once('end', () => this.failAll(this.failure('read pipe', new Error('stream ended'))));
    this.readPipe.once('close', () => this.failAll(this.failure('read pipe', new Error('stream closed'))));
    this.writePipe.once('close', () => this.failAll(this.failure('write pipe', new Error('stream closed'))));
    child.once('error', (error) => this.failAll(this.failure('process launch', error)));
    child.once('close', (code, signal) => {
      const ending = signal ? `signal ${signal}` : `exit code ${code}`;
      this.failAll(this.failure('process exit', new Error(`Chrome closed with ${ending}`)));
    });
  }

  failure(stage, error) {
    const code = error?.code ? ` [${error.code}]` : '';
    const details = this.failureDetails();
    const failure = new Error([
      `Chrome DevTools ${stage} failed: ${error?.message || String(error)}${code}`,
      details,
      `Chrome transport: Node ${process.version}, libuv ${process.versions.uv}, ${process.platform}; pid=${this.child.pid ?? 'unavailable'}, exitCode=${this.child.exitCode}, signalCode=${this.child.signalCode}.`,
      `Chrome read pipe: readable=${this.readPipe.readable}, ended=${this.readPipe.readableEnded}, destroyed=${this.readPipe.destroyed}, receivedBytes=${this.receivedBytes}, messages=${this.receivedMessages}, bufferedBytes=${Buffer.byteLength(this.buffer)}.`,
      `Chrome write pipe: writable=${this.writePipe.writable}, ended=${this.writePipe.writableEnded}, destroyed=${this.writePipe.destroyed}, completedWrites=${this.completedWrites}, writtenBytes=${this.writtenBytes}, bufferedBytes=${this.writePipe.writableLength}.`,
    ].filter(Boolean).join('\n'));
    if (error?.code) failure.code = error.code;
    if (error?.method) failure.method = error.method;
    return failure;
  }

  consume(chunk) {
    this.receivedBytes += Buffer.byteLength(chunk);
    this.buffer += chunk;
    let boundary;
    while ((boundary = this.buffer.indexOf('\0')) >= 0) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 1);
      if (!raw) continue;
      let message;
      try {
        message = JSON.parse(raw);
      } catch (error) {
        this.failAll(new Error(`Chrome DevTools returned invalid JSON: ${error.message}`));
        continue;
      }
      this.receivedMessages += 1;
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) continue;
        clearTimeout(pending.timer);
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(`${pending.method}: ${message.error.message}`));
        else pending.resolve(message.result || {});
        continue;
      }
      for (const waiter of [...this.waiters]) {
        if (waiter.method !== message.method) continue;
        if (waiter.sessionId && waiter.sessionId !== message.sessionId) continue;
        clearTimeout(waiter.timer);
        this.waiters.splice(this.waiters.indexOf(waiter), 1);
        waiter.resolve(message.params || {});
      }
    }
  }

  send(method, params = {}, sessionId = undefined, timeoutMs = 15000) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        const error = new Error(`${method}: timed out after ${timeoutMs}ms`);
        error.code = 'ERR_CHROME_CDP_TIMEOUT';
        error.method = method;
        reject(this.failure('protocol timeout', error));
      }, timeoutMs);
      this.pending.set(id, { method, resolve, reject, timer });
      try {
        const serialized = `${JSON.stringify(message)}\0`;
        this.writePipe.write(serialized, (error) => {
          if (error) {
            this.failAll(this.failure('write pipe', error));
          } else {
            this.completedWrites += 1;
            this.writtenBytes += Buffer.byteLength(serialized);
          }
        });
      } catch (error) {
        this.failAll(this.failure('write pipe', error));
      }
    });
  }

  waitFor(method, sessionId, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const waiter = { method, sessionId, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        this.waiters.splice(this.waiters.indexOf(waiter), 1);
        reject(this.failure('event timeout', new Error(`${method}: event timed out after ${timeoutMs}ms`)));
      }, timeoutMs);
      this.waiters.push(waiter);
    });
  }

  failAll(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    for (const waiter of this.waiters) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    this.pending.clear();
    this.waiters = [];
  }
}

export class ChromeVisualBrowser {
  constructor(chromePath, {
    env = process.env,
    getuid = typeof process.getuid === 'function' ? () => process.getuid() : null,
    spawnImpl = spawn,
    startupTimeoutMs = CHROME_STARTUP_TIMEOUT_MS,
  } = {}) {
    this.profileRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'archify-website-browser-profile-'));
    this.stderr = '';
    const args = [
      '--headless=new',
      '--remote-debugging-pipe',
      '--disable-gpu',
      '--hide-scrollbars',
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-default-apps',
      '--disable-sync',
      '--metrics-recording-only',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
      '--force-device-scale-factor=1',
      `--user-data-dir=${this.profileRoot}`,
      'about:blank',
    ];
    const rootUser = typeof getuid === 'function' && getuid() === 0;
    const sandboxOptOut = env?.[CHROME_NO_SANDBOX_ENV] === '1';
    if (rootUser || sandboxOptOut) args.unshift('--no-sandbox');
    this.child = spawnImpl(chromePath, args, { stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'] });
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (chunk) => {
      this.stderr = `${this.stderr}${chunk}`.slice(-8000);
    });
    this.child.stderr.on('error', (error) => {
      this.stderr = `${this.stderr}\nChrome stderr stream failed: ${error.message}`.trim().slice(-8000);
    });
    this.cdp = new PipeCdp(this.child, {
      failureDetails: () => {
        const exit = this.child.signalCode
          ? `signal ${this.child.signalCode}`
          : this.child.exitCode == null ? 'still running' : `exit code ${this.child.exitCode}`;
        const stderr = this.stderr.trim();
        return [
          `Chrome process: ${exit}.`,
          stderr ? `Chrome stderr:\n${stderr}` : '',
        ].filter(Boolean).join('\n');
      },
    });
    this.startupTimeoutMs = startupTimeoutMs;
    this.sessionPromise = this.attach();
  }

  async attach() {
    const targets = await this.cdp.send(
      'Target.getTargets', {}, undefined, this.startupTimeoutMs,
    );
    let target = targets.targetInfos?.find((item) => item.type === 'page');
    if (!target) {
      const created = await this.cdp.send('Target.createTarget', { url: 'about:blank' });
      target = { targetId: created.targetId };
    }
    const attached = await this.cdp.send('Target.attachToTarget', {
      targetId: target.targetId,
      flatten: true,
    });
    await this.cdp.send('Page.enable', {}, attached.sessionId);
    await this.cdp.send('Runtime.enable', {}, attached.sessionId);
    return attached.sessionId;
  }

  close() {
    if (!this.closePromise) this.closePromise = this.finishClose();
    return this.closePromise;
  }

  async finishClose() {
    this.cdp.failAll(new Error('website browser finished'));
    try {
      if (this.child.exitCode === null && this.child.signalCode === null) {
        await new Promise((resolve) => {
          let timer = null;
          let settled = false;
          const finish = () => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            this.child.removeListener('exit', finish);
            resolve();
          };
          this.child.once('exit', finish);
          this.child.kill('SIGTERM');
          if (settled) return;
          timer = setTimeout(() => {
            if (this.child.exitCode === null && this.child.signalCode === null) this.child.kill('SIGKILL');
            finish();
          }, 1500);
        });
      }
    } finally {
      for (const stream of [this.child.stderr, this.child.stdio[3], this.child.stdio[4]]) {
        if (stream && !stream.destroyed) stream.destroy();
      }
    }
    try {
      fs.rmSync(this.profileRoot, { recursive: true, force: true });
    } catch {
      // Chrome may briefly retain profile files on Windows; the browser test is done.
    }
  }
}
