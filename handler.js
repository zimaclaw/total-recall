import { execSync } from 'node:child_process';
import { writeFileSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';

const LOG = '/tmp/total-recall.log';
const MEMORY_DIR = '/home/ironman/.openclaw/skills/memory-reflect';
const PYTHON = '/home/ironman/.openclaw/skills/memory-reflect/.venv/bin/python';
const SCRIPT = '/home/ironman/.openclaw/skills/memory-reflect/memory-reflect.py';
const SESSION_STORE = '/home/ironman/.openclaw/skills/memory-reflect/session_store.py';
const CORE_MD = '/home/ironman/.openclaw/workspace/CORE.md';

// Глобальная переменная для sessionId
let lastKnownSessionId = null;

function log(msg) {
  writeFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`, { flag: 'a' });
}

function inferCategory(prompt) {
  const text = (prompt || '').toLowerCase();
  
  // Приоритетные категории — проверяем первыми
  // Если есть явное упоминание deployment/deploy service — это deploy, не infra
  const deployPatterns = [
    /deploy\s+(gateway|service|app|version|new)/,  // "deploy gateway", "deploy new version"
    /задеплой/,  // русское "задеплой"
    /deployment/,
    /release\s+(new|latest|v\d)/,  // "release new", "release v1.0"
  ];
  
  for (const pattern of deployPatterns) {
    if (pattern.test(text)) {
      return 'deploy';
    }
  }
  
  const keywords = {
    infra: ['server', 'docker', 'nginx', 'port', 'network', 'сервер', 'порт', 'systemd', 'nginx', 'reverse', 'proxy'],
    dev: ['code', 'script', 'bug', 'fix', 'function', 'git', 'код', 'скрипт', 'баг', 'python', 'node'],
    memory: ['memory', 'remember', 'flashback', 'reflect', 'память', 'вспомни', 'принцип'],
    research: ['research', 'find', 'search', 'analyze', 'исследуй', 'найди', 'поищи'],
    test: ['test', 'check', 'validate', 'verify', 'тест', 'проверь'],
    deploy: ['deploy', 'release', 'publish', 'деплой', 'релиз', 'deployments', 'ci', 'cd', 'pipeline'],
    plan: ['plan', 'schedule', 'roadmap', 'план', 'роадмап'],
    write: ['write', 'document', 'report', 'напиши', 'документ'],
  };
  
  const scores = {};
  for (const [cat, kws] of Object.entries(keywords)) {
    scores[cat] = kws.filter(kw => text.includes(kw)).length;
  }
  
  const best = Object.entries(scores).sort((a, b) => b[1] - a[1])[0];
  return best[1] > 0 ? best[0] : 'dev';
}

function runFlashback(category) {
  const cmd = `${PYTHON} ${SCRIPT} --flashback --category ${category}`;
  try {
    return execSync(cmd, { encoding: 'utf8', timeout: 10000, cwd: MEMORY_DIR });
  } catch (err) {
    log(`flashback error: ${err.message}`);
    return '';
  }
}

function formatContext(raw, category) {
  if (!raw?.trim()) return '';
  
  const lines = raw.split('\n')
    .filter(l => !/\d{4}-\d{2}-\d{2}.*\[(INFO|WARNING|ERROR)\]/.test(l) && l.trim())
    .join('\n').trim();
  
  if (!lines) return '';
  
  return `=== MEMORY CONTEXT [${category}] ===\n${lines}\n=== END MEMORY CONTEXT ===`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function runPython(script, args, timeoutMs = 8000) {
  try {
    const result = execSync(PYTHON, [script, ...args], {
      timeout: timeoutMs,
      cwd: MEMORY_DIR,
      encoding: 'utf8',
    });
    // Node v22 может возвращать Buffer вместо строки
    const str = Buffer.isBuffer(result) ? result.toString('utf8') : String(result);
    return str.trim();
  } catch (err) {
    log(`ERROR ${script} ${args[0]}: ${err.message}`);
    return null;
  }
}

function parseJson(out) {
  try { return JSON.parse(out); } catch { return null; }
}

function resolveSessionId(ctx, event) {
  if (ctx?.sessionId) return ctx.sessionId;
  if (lastKnownSessionId) return lastKnownSessionId;
  if (ctx?.conversationId) return ctx.conversationId;
  try {
    const storePath = `${homedir()}/.openclaw/agents/main/sessions/sessions.json`;
    const store = JSON.parse(readFileSync(storePath, 'utf8'));
    for (const [key, entry] of Object.entries(store)) {
      if (entry?.sessionId && key.includes('tui-')) {
        return entry.sessionId;
      }
    }
    const first = Object.values(store).find(e => e?.sessionId);
    if (first) return first.sessionId;
  } catch(e) {
    log('resolveSessionId error: ' + e.message);
  }
  return null;
}

function getCoremd() {
  try { return readFileSync(CORE_MD, 'utf8').trim(); } catch { return null; }
}

function getFlashback(prompt) {
  const out = runPython(SCRIPT, ['--flashback', '--query', prompt]);
  if (!out?.trim()) return null;
  const lines = out.split('\n')
    .filter(l => !/\d{4}-\d{2}-\d{2}.*\[(INFO|WARNING|ERROR)\]/.test(l) && l.trim())
    .join('\n').trim();
  return lines ? { text: lines } : null;
}

function getSkeleton(sessionId) {
  const out = runPython(SESSION_STORE, ['get_skeleton', '--session-id', sessionId]);
  const data = parseJson(out);
  if (!data) return null;
  
  const parts = [];
  if (data.summary) {
    parts.push('[SUMMARY]');
    parts.push(data.summary);
    parts.push('');
  }
  parts.push('[RECENT]');
  parts.push(data.tail);
  
  return parts.join('\n');
}

function getFocus(sessionId, prompt) {
  const out = runPython(SESSION_STORE, ['focus', '--session-id', sessionId, '--query', prompt]);
  const data = parseJson(out);
  return data?.focus || null;
}

function block(title, content) {
  return `=== ${title} ===\n${content}\n=== END ${title} ===`;
}

export async function beforePromptBuild(event, ctx) {
  const t0 = Date.now();
  
  if (ctx?.sessionId) lastKnownSessionId = ctx.sessionId;
  const userPrompt = event?.prompt || '';
  if (!userPrompt) return {};

  const sessionId = resolveSessionId(ctx, event);
  log(`before_prompt_build: sessionKey=${ctx?.sessionKey} sessionId=${ctx?.sessionId} agentId=${ctx?.agentId}`);

  const stableParts = [];
  const coremd = getCoremd();
  if (coremd) stableParts.push(block('CORE', coremd));

  const fb = getFlashback(userPrompt);
  if (fb) stableParts.push(block('MEMORY CONTEXT', fb.text));

  if (sessionId) {
    const skeleton = getSkeleton(sessionId);
    if (skeleton) stableParts.push(block('SESSION SKELETON', skeleton));

    const focus = getFocus(sessionId, userPrompt);
    if (focus) stableParts.push(block('SESSION FOCUS', focus));
  }

  const totalParts = stableParts.length;
  log(`before_prompt_build: ${Date.now() - t0}ms | parts=${totalParts} session=${sessionId || 'none'}`);

  const result = {};
  if (stableParts.length) {
    result.prependSystemContext = stableParts.join('\n\n');
    
    const debugMode = process.env.TOTAL_RECALL_DEBUG === '1';
    if (debugMode) {
      const hasSkeleton = result.prependSystemContext.includes('SESSION SKELETON');
      const hasFocus = result.prependSystemContext.includes('SESSION FOCUS');
      const hasCore = result.prependSystemContext.includes('CORE.md');
      const hasMemory = result.prependSystemContext.includes('MEMORY CONTEXT');
      log(`DEBUG: prependSystemContext contains: CORE=${hasCore}, MEMORY=${hasMemory}, SKELETON=${hasSkeleton}, FOCUS=${hasFocus}`);
      
      if (hasSkeleton) {
        const skeletonStart = result.prependSystemContext.indexOf('=== SESSION SKELETON ===');
        if (skeletonStart !== -1) {
          const skeletonEnd = result.prependSystemContext.indexOf('=== END SESSION SKELETON ===');
          if (skeletonEnd !== -1) {
            const skeletonContent = result.prependSystemContext.substring(skeletonStart, skeletonEnd + 28);
            log(`DEBUG: SESSION SKELETON content (${skeletonContent.length} chars):\n${skeletonContent.substring(0, 500)}${skeletonContent.length > 500 ? '...' : ''}`);
          }
        }
      }
    }
  }
  return result;
}
