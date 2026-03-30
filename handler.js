import { execFileSync } from 'node:child_process';
import { readFileSync, appendFileSync } from 'node:fs';

const LOG     = '/tmp/total-recall.log';
const DIR     = '/home/ironman/.openclaw/skills/memory-reflect';
const PYTHON  = `${DIR}/.venv/bin/python`;
const REFLECT = `${DIR}/memory-reflect.py`;
const SESSION = `${DIR}/session_store.py`;
const CORE_MD = '/home/ironman/.openclaw/workspace/CORE.md';

// In-memory маппинг составного ключа → sessionId
const sessionMap = new Map();

function log(msg) {
  appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`);
}

function runPython(script, args, timeoutMs = 8000) {
  try {
    return execFileSync(PYTHON, [script, ...args], {
      timeout: timeoutMs,
      cwd: DIR,
      encoding: 'utf8',
    }).trim();
  } catch (err) {
    log(`ERROR ${script} ${args[0]}: ${err.message}`);
    return null;
  }
}

function parseJson(out) {
  try { return JSON.parse(out); } catch { return null; }
}

// ─── Ключ сессии ─────────────────────────────────────────────────────────
// before_prompt_build: ctx.sessionId (UUID)
// message_received:   нет sessionId, используем channelId:senderId
// Они не совпадают — используем sessionId когда есть, иначе составной ключ

function resolveSessionId(ctx, event) {
  if (ctx?.sessionId) return ctx.sessionId;
  if (ctx?.conversationId) return ctx.conversationId;
  // Читаем sessionId из sessions.json по sessionKey
  try {
    const { readFileSync } = require('node:fs');
    const { homedir } = require('node:os');
    const storePath = `${homedir()}/.openclaw/agents/main/sessions/sessions.json`;
    const store = JSON.parse(readFileSync(storePath, 'utf8'));
    // Пробуем все ключи — берём первый у которого есть sessionId
    const channelId = ctx?.channelId || 'webchat';
    for (const [key, entry] of Object.entries(store)) {
      if (entry?.sessionId && key.includes(channelId)) {
        return entry.sessionId;
      }
    }
    // Fallback — берём первый sessionId
    const first = Object.values(store).find(e => e?.sessionId);
    if (first) return first.sessionId;
  } catch(e) {
    log('resolveSessionId error: ' + e.message);
  }
  // Последний fallback — составной ключ
  const channelId = ctx?.channelId;
  const senderId = event?.metadata?.senderId || event?.from;
  if (channelId && senderId) return `${channelId}:${senderId}`;
  return null;
}

// ─── Категоризация ───────────────────────────────────────────────────────

const DEPLOY_PATTERNS = [
  /deploy\s+(gateway|service|app|version|new)/,
  /задеплой/, /deployment/,
  /release\s+(new|latest|v\d)/,
];

const KEYWORDS = {
  infra:    ['server', 'docker', 'nginx', 'port', 'network', 'сервер', 'порт', 'systemd', 'proxy'],
  dev:      ['code', 'script', 'bug', 'fix', 'function', 'git', 'код', 'скрипт', 'баг', 'python', 'node'],
  memory:   ['memory', 'remember', 'flashback', 'reflect', 'память', 'вспомни', 'принцип'],
  research: ['research', 'find', 'search', 'analyze', 'исследуй', 'найди', 'поищи'],
  deploy:   ['deploy', 'release', 'publish', 'деплой', 'релиз', 'ci', 'cd', 'pipeline'],
  data:     ['database', 'query', 'postgres', 'neo4j', 'qdrant', 'redis', 'база', 'таблица'],
};

function inferCategory(prompt) {
  const text = (prompt || '').toLowerCase();
  for (const p of DEPLOY_PATTERNS) {
    if (p.test(text)) return 'deploy';
  }
  const scores = Object.entries(KEYWORDS).map(([cat, kws]) => ({
    cat, score: kws.filter(kw => text.includes(kw)).length,
  }));
  scores.sort((a, b) => b.score - a.score);
  return scores[0].score > 0 ? scores[0].cat : 'dev';
}

// ─── Источники контекста ─────────────────────────────────────────────────

function getCoremd() {
  try { return readFileSync(CORE_MD, 'utf8').trim(); } catch { return null; }
}

function getFlashback(prompt) {
  const category = inferCategory(prompt);
  const out = runPython(REFLECT, ['--flashback', '--category', category]);
  if (!out?.trim()) return null;
  const lines = out.split('\n')
    .filter(l => !/\d{4}-\d{2}-\d{2}.*\[(INFO|WARNING|ERROR)\]/.test(l) && l.trim())
    .join('\n').trim();
  return lines ? { text: lines, category } : null;
}

function getSkeleton(sessionId) {
  const out = runPython(SESSION, ['skeleton', '--session-id', sessionId]);
  const data = parseJson(out);
  return data?.skeleton || null;
}

function getFocus(sessionId, prompt) {
  const out = runPython(SESSION, ['focus', '--session-id', sessionId, '--query', prompt]);
  const data = parseJson(out);
  return data?.focus || null;
}

function block(title, content) {
  return `=== ${title} ===\n${content}\n=== END ${title} ===`;
}

// ─── Хуки ────────────────────────────────────────────────────────────────

export function onCommandNew(event, ctx) {
  const sessionId = resolveSessionId(ctx, event);
  if (!sessionId) return;
  log(`command:new → session: ${sessionId}`);
  runPython(SESSION, ['session_start', '--session-id', sessionId]);
}

export function onMessageReceived(event, ctx) {
  const content = event?.content;
  const sessionId = resolveSessionId(ctx, event);
  log(`message_received: sessionId=${sessionId} content=${!!content}`);
  if (!content || !sessionId) return;
  runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
  runPython(SESSION, [
    'message_write',
    '--session-id', sessionId,
    '--role', 'user',
    '--content', content,
  ], 12000);
}

export async function beforePromptBuild(event, ctx) {
  const t0 = Date.now();
  const userPrompt = event?.prompt || '';
  if (!userPrompt) return {};

  const sessionId = resolveSessionId(ctx, event);
  log(`before_prompt_build: sessionKey=${ctx?.sessionKey} sessionId=${ctx?.sessionId} agentId=${ctx?.agentId}`);
  // Сохраняем маппинг channelId:senderId → sessionId для message_received
  if (ctx?.sessionId && ctx?.channelId) {
    const compositeKey = `${ctx.channelId}:gateway-client`;
    if (!sessionMap.has(compositeKey)) {
      sessionMap.set(compositeKey, ctx.sessionId);
    }
  }
  const parts = [];

  const coremd = getCoremd();
  if (coremd) parts.push(block('CORE', coremd));

  const fb = getFlashback(userPrompt);
  if (fb) parts.push(block(`MEMORY CONTEXT [${fb.category}]`, fb.text));

  if (sessionId) {
    const skeleton = getSkeleton(sessionId);
    if (skeleton) parts.push(block('SESSION SKELETON', skeleton));

    const focus = getFocus(sessionId, userPrompt);
    if (focus) parts.push(block('SESSION FOCUS', focus));
  }

  log(`before_prompt_build: ${Date.now() - t0}ms | parts=${parts.length} session=${sessionId || 'none'}`);

  if (!parts.length) return {};
  return { prependContext: parts.join('\n\n') };
}

export function onMessageSent(event, ctx) {
  const content = event?.content;
  const sessionId = resolveSessionId(ctx, event);
  if (!content || !sessionId) return;
  runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
  runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
  runPython(SESSION, [
    'message_write',
    '--session-id', sessionId,
    '--role', 'assistant',
    '--content', content,
  ], 5000);
}
