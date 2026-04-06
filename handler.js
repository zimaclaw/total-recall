import { execFileSync } from 'node:child_process';
import { readFileSync, appendFileSync } from 'node:fs';
import { homedir } from 'node:os';

const LOG     = '/tmp/total-recall.log';
const DIR     = '/home/ironman/.openclaw/skills/memory-reflect';
const PYTHON  = `${DIR}/.venv/bin/python`;
const REFLECT = `${DIR}/memory-reflect.py`;
const SESSION = `${DIR}/session_store.py`;
const KB_STORE = `${DIR}/kb_store.py`;
const CORE_MD = '/home/ironman/.openclaw/workspace/CORE.md';

// In-memory маппинг составного ключа → sessionId
const sessionMap = new Map();

let lastKnownSessionId = null; // Глобальная — последний известный правильный sessionId

// In-memory буфер pending user messages для pair_write
const pendingUserMessages = new Map(); // sessionId → content

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
  if (lastKnownSessionId) return lastKnownSessionId;
  if (ctx?.conversationId) return ctx.conversationId;
  // Читаем sessionId из sessions.json по sessionKey
  try {
    const storePath = `${homedir()}/.openclaw/agents/main/sessions/sessions.json`;
    const store = JSON.parse(readFileSync(storePath, 'utf8'));
    // Приоритет 1: активная TUI сессия
    for (const [key, entry] of Object.entries(store)) {
      if (entry?.sessionId && key.includes('tui-')) {
        return entry.sessionId;
      }
    }
    // Приоритет 2: поиск по channelId
    const channelId = ctx?.channelId || 'webchat';
    for (const [key, entry] of Object.entries(store)) {
      if (entry?.sessionId && key.includes(channelId)) {
        return entry.sessionId;
      }
    }
    // Fallback — первый найденный
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
  const out = runPython(REFLECT, ['--flashback', '--query', prompt]);
  if (!out?.trim()) return null;
  const lines = out.split('\n')
    .filter(l => !/\d{4}-\d{2}-\d{2}.*\[(INFO|WARNING|ERROR)\]/.test(l) && l.trim())
    .join('\n').trim();
  return lines ? { text: lines } : null;
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

/**
 * Поиск релевантной информации в Knowledge Base.
 * @param {string} prompt - запрос пользователя
 * @returns {string|null} форматированный список результатов или null
 */
function getKB(prompt) {
  const out = runPython(KB_STORE, ['kb_search', '--query', prompt, '--limit', '3'], 10000);
  const data = parseJson(out);
  if (!data?.results?.length) return null;
  const lines = data.results
    .filter(r => r.score > 0.55)
    .map(r => `[${r.title}]\n${r.summary}`)
    .join('\n\n');
  return lines || null;
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
  // Сохраняем sessionId + content вместе для последующего pair_write
  pendingUserMessages.set('current', { sessionId, content });
  log(`pending_user: sessionId=${sessionId} saved (${content.length} chars)`);
}

export async function beforePromptBuild(event, ctx) {
  const t0 = Date.now();
  
  if (ctx?.sessionId) lastKnownSessionId = ctx.sessionId;
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

  // Стабильный контекст (prependSystemContext) — CORE.md + flashback
  const stableParts = [];
  const coremd = getCoremd();
  if (coremd) stableParts.push(block('CORE', coremd));

  const fb = getFlashback(userPrompt);
  if (fb) stableParts.push(block('MEMORY CONTEXT', fb.text));

  // Динамический контекст (prependContext) — скелет + фокус + KB
  const dynamicParts = [];
  if (sessionId) {
    const skeleton = getSkeleton(sessionId);
    if (skeleton) dynamicParts.push(block('SESSION SKELETON', skeleton));

    const focus = getFocus(sessionId, userPrompt);
    if (focus) dynamicParts.push(block('SESSION FOCUS', focus));
  }

  // KB — релевантная информация из Knowledge Base
  const kb = getKB(userPrompt);
  if (kb) dynamicParts.push(block('KNOWLEDGE BASE', kb));

  const stableCount = stableParts.length;
  const dynamicCount = dynamicParts.length;
  log(`before_prompt_build: ${Date.now() - t0}ms | stable=${stableCount} dynamic=${dynamicCount} session=${sessionId || 'none'}`);

  const result = {};
  if (stableParts.length) {
    result.prependSystemContext = stableParts.join('\n\n');
  }
  if (dynamicParts.length) {
    result.prependContext = dynamicParts.join('\n\n');
  }
  return result;
}

export function onMessageSent(event, ctx) {
  // agent_end event: event.messages — массив всех сообщений сессии
  // Берём последнее assistant сообщение
  const lastAssistant = [...event.messages].reverse().find(m => m.role === 'assistant');
  const content = lastAssistant?.content?.find(c => c.type === 'text')?.text;
  
  log(`onMessageSent: sessionId=${ctx?.sessionId} messages=${event?.messages?.length || 0} content=${!!content}`);
  
  // Берём pending из буфера (там sessionId + content)
  const pending = pendingUserMessages.get('current');
  pendingUserMessages.delete('current');
  
  if (pending) {
    const sessionId = pending.sessionId;
    const pendingUser = pending.content;
    
    if (!content || !sessionId) {
      log(`pair_write: skip — content=${!!content} sessionId=${sessionId}`);
      return;
    }
    
    runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
    
    // pair_write — создаём пару user+assistant
    log(`pair_write: sessionId=${sessionId} user=${pendingUser.length} assistant=${content.length}`);
    runPython(SESSION, [
      'pair_write',
      '--session-id', sessionId,
      '--user-content', pendingUser,
      '--assistant-content', content,
    ], 15000);
  } else {
    // Fallback — нет pending user, используем resolveSessionId
    const sessionId = event?.sessionId || ctx?.sessionId || resolveSessionId(ctx, event);
    
    if (!content || !sessionId) {
      log(`before_message_write: skip message_write — content=${!!content} sessionId=${sessionId}`);
      return;
    }
    
    runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
    
    // Fallback — пишем как одиночное сообщение
    log(`pair_write: no pending user for ${sessionId} fallback to message_write`);
    runPython(SESSION, [
      'message_write',
      '--session-id', sessionId,
      '--role', 'assistant',
      '--content', content,
    ], 5000);
  }
}
