import { execFileSync } from 'node:child_process';
import { readFileSync, appendFileSync } from 'node:fs';
import { homedir } from 'node:os';

const OPENCLAW_CONFIG_PATH = process.env.OPENCLAW_CONFIG_PATH || `${homedir()}/.openclaw/openclaw.json`;

// ─── Чтение конфига total-recall из openclaw.json ───────────────────────────
function getTotalRecallConfig() {
  try {
    const config = JSON.parse(readFileSync(OPENCLAW_CONFIG_PATH, 'utf8'));
    return config?.plugins?.entries?.['total-recall']?.config || {};
  } catch (err) {
    return {};
  }
}

// Кэширование конфига (вычисляем один раз при старте)
const TR_CONFIG = getTotalRecallConfig();

// Пути с fallback на конфиг → env → хардкод
const LOG     = TR_CONFIG.paths?.log || '/tmp/total-recall.log';
const DIR     = TR_CONFIG.paths?.memoryReflect || process.env.MEMORY_REFLECT_DIR || `${homedir()}/.openclaw/skills/memory-reflect`;
const PYTHON  = `${DIR}/.venv/bin/python`;
const REFLECT = `${DIR}/memory-reflect.py`;
const SESSION = `${DIR}/session_store.py`;
const KB_STORE = `${DIR}/kb_store.py`;
const CORE_MD = TR_CONFIG.paths?.coreMd || process.env.CORE_MD_PATH || `${homedir()}/.openclaw/workspace/CORE.md`;
const CURATOR_DEFAULT_CONTEXT = TR_CONFIG.curator?.defaultContext || parseInt(process.env.CURATOR_DEFAULT_CONTEXT) || 32000;

// ─── Curator бюджет ─────────────────────────────────────────────────────────
// Чтение contextWindow из openclaw.json
function getCuratorBudget() {
  try {
    const config = JSON.parse(readFileSync(OPENCLAW_CONFIG_PATH, 'utf8'));
    // Пятница использует llamacpp
    const provider = config?.models?.providers?.llamacpp;
    if (!provider?.models?.[0]) {
      log(`Curator budget: fallback to ${CURATOR_DEFAULT_CONTEXT} (no llamacpp model)`);
      return CURATOR_DEFAULT_CONTEXT;
    }
    const contextWindow = provider.models[0].contextWindow;
    const maxTokens = provider.models[0].maxTokens || 0;
    const reserveTokensFloor = config?.agents?.defaults?.compaction?.reserveTokensFloor || 30000;
    const budget = contextWindow - maxTokens - reserveTokensFloor;
    log(`Curator budget: contextWindow=${contextWindow} - maxTokens=${maxTokens} - reserve=${reserveTokensFloor} = ${budget}`);
    return budget;
  } catch (err) {
    log(`Curator budget: error reading ${OPENCLAW_CONFIG_PATH}: ${err.message}, fallback to ${CURATOR_DEFAULT_CONTEXT}`);
    return CURATOR_DEFAULT_CONTEXT;
  }
}

// Кэширование бюджета (вычисляем один раз при старте)
const CURATOR_BUDGET = getCuratorBudget();

// In-memory маппинг составного ключа → sessionId
const sessionMap = new Map();

let lastKnownSessionId = null; // Глобальная — последний известный правильный sessionId

// In-memory буфер pending user messages для pair_write
const pendingUserMessages = new Map(); // sessionId → content

function log(msg) {
  appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`);
}

function runPython(script, args, timeoutMs = 5000) {
  // Передаём очищенный env — Python скрипты работают только с локальной сетью (.145)
  // ALL_PROXY (singbox/SOCKS) не нужен и вызывает 502/ETIMEDOUT для локальных вызовов
  const env = { ...process.env };
  // Обнуляем (не удаляем) — httpx различает отсутствие ключа и пустую строку
  env.ALL_PROXY = '';
  env.all_proxy = '';
  env.HTTPS_PROXY = '';
  env.https_proxy = '';
  env.HTTP_PROXY = '';
  env.http_proxy = '';
  try {
    return execFileSync(PYTHON, [script, ...args], {
      timeout: timeoutMs,
      cwd: DIR,
      encoding: 'utf8',
      env,
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
  const out = runPython(SESSION, ['skeleton', '--session-id', sessionId], 10000);
  const data = parseJson(out);
  const summary = data?.summary || null;
  const tail = data?.tail || null;
  if (!tail) return null;
  if (summary) return `[SUMMARY]\n${summary}\n\n[RECENT]\n${tail}`;
  return tail;
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
  const scoreThreshold = TR_CONFIG.kb?.scoreThreshold || 0.55;
  const maxResults = TR_CONFIG.kb?.maxResults || 3;
  
  const out = runPython(KB_STORE, ['kb_search', '--query', prompt, '--limit', maxResults.toString()], 10000);
  const data = parseJson(out);
  if (!data?.results?.length) return null;
  const lines = data.results
    .filter(r => r.score > scoreThreshold)
    .slice(0, maxResults)
    .map(r => `[${r.title}]\n${r.summary}`)
    .join('\n\n');
  return lines || null;
}

function block(title, content) {
  return `=== ${title} ===\n${content}\n=== END ${title} ===`;
}

// ─── Команды KB ───────────────────────────────────────────────────────────

/**
 * Обработка всех команд KB
 * @param {string} args - аргументы команды (после /kb)
 * @param {string} sessionId - ID сессии
 * @param {object} event - event объект (для res())
 * @returns {object|null} результат обработки или null
 */
function handleKbCommands(args, sessionId, event) {
  const parts = args.split(' ');
  const command = parts[0];
  const rest = parts.slice(1).join(' ');
  
  switch (command) {
    case 'handoff':
      return handleKbHandoff(rest, sessionId, event);
    
    case 'handoffs':
      return handleKbHandoffsList(rest);
    
    case 'load':
      return handleKbLoad(rest, sessionId, event);
    
    case 'help':
      return { text: getKbHelp() };
    
    default:
      return { text: `❌ Неизвестная команда KB: ${command}\n\n${getKbHelp()}` };
  }
}

/**
 * Создание handoff сессии
 * @param {string} args - аргументы (описание + --new-session)
 * @param {string} sessionId - ID сессии
 * @param {object} event - event объект
 * @returns {object} результат
 */
function handleKbHandoff(args, sessionId, event) {
  // Парсинг аргументов
  const newSession = args.includes('--new-session');
  let description = args;
  if (newSession) {
    description = args.replace('--new-session', '').trim();
  }
  description = description.trim().replace(/^["']|["']$/g, ''); // Удалить кавычки
  
  // Валидация
  if (!description || description.length < 5) {
    return {
      text: "❌ Укажи краткое описание результата (минимум 5 символов)\n\nПример:\n/kb handoff \"Решена проблема с портом 8080\"\n/kb handoff \"Исследовал архитектуру\" --new-session"
    };
  }
  
  log(`handleKbHandoff: description="${description}" newSession=${newSession}`);
  
  // 1. Получить сообщения сессии
  const messages = getSessionMessagesForHandoff(sessionId);
  if (!messages || messages.length === 0) {
    return { text: "❌ Нет сообщений в сессии для сохранения" };
  }
  
  // 2. Отбор сообщений (семантический или last N)
  const selectedMessages = selectMessagesForHandoff(messages);
  
  // 3. Форматирование контента
  const content = formatMessagesForHandoff(selectedMessages);
  
  // 4. Сохранение в KB
  const result = saveToKb(description, content, sessionId);
  if (!result || !result.id) {
    return { text: "❌ Ошибка сохранения в KB" };
  }
  
  // 5. Если --new-session — создать новую сессию
  let newSessionText = '';
  if (newSession) {
    createNewSession(sessionId);
    newSessionText = '\n\n✅ Новая сессия создана.';
  }
  
  // 6. Ответ пользователю
  return {
    text: `✅ Сессия сохранена в KB\n\n` +
          `Title: ${description}\n` +
          `Messages: ${selectedMessages.length}\n` +
          `KB ID: ${result.id}\n` +
          `Category: session\n` +
          newSessionText
  };
}

/**
 * Лист handoffs
 * @param {string} args - аргументы (--limit N, --search "query")
 * @returns {object} результат
 */
function handleKbHandoffsList(args) {
  // Парсинг аргументов
  let limit = 20;
  let search = null;
  
  const limitMatch = args.match(/--limit\s+(\d+)/);
  if (limitMatch) {
    limit = parseInt(limitMatch[1], 10);
  }
  
  const searchMatch = args.match(/--search\s+["']?([^"'\s]+)["']?/);
  if (searchMatch) {
    search = searchMatch[1];
  }
  
  // Получение handoffs из KB
  const handoffs = getHandoffsFromKb(limit, search);
  if (!handoffs || handoffs.length === 0) {
    return { text: "Нет handoffs в KB" };
  }
  
  // Форматирование вывода
  let output = `Handoffs (последние ${handoffs.length}):\n\n`;
  handoffs.forEach((h, idx) => {
    const num = idx + 1;
    const date = new Date(h.created_at).toISOString().replace('T', ' ').substring(0, 19);
    output += `#${num} | ${date} UTC | ${h.title}\n`;
  });
  
  output += `\nКоманды:\n`;
  output += `  /kb handoff <id>     — детали handoff\n`;
  output += `  /kb load <id>        — загрузить в контекст\n`;
  output += `  /kb delete <id>      — удалить\n`;
  output += `  /kb promote <id>     — промотировать в cold\n`;
  
  return { text: output };
}

/**
 * Загрузка handoff в контекст
 * @param {string} args - аргументы (id или номер или частичное совпадение)
 * @param {string} sessionId - ID сессии
 * @param {object} event - event объект
 * @returns {object} результат
 */
function handleKbLoad(args, sessionId, event) {
  if (!args) {
    return { text: "❌ Укажи ID handoff\n\nПример:\n/kb load 1\n/kb load \"архитектура\"" };
  }
  
  // Парсинг ID (номер, id, или частичное совпадение)
  const handoff = findHandoffById(args);
  if (!handoff) {
    return { text: `❌ Handoff не найден: ${args}` };
  }
  
  // Форматирование контекста
  const context = formatHandoffContext(handoff);
  
  // Добавление в prependSystemContext (через event)
  if (event?.prependSystemContext) {
    event.prependSystemContext = context + '\n\n' + event.prependSystemContext;
  }
  
  // Ответ пользователю
  const date = new Date(handoff.created_at).toISOString().replace('T', ' ').substring(0, 19);
  return {
    text: `✅ Handoff загружен в контекст\n\n` +
          `Title: ${handoff.title}\n` +
          `Date: ${date} UTC\n` +
          `Size: ~${estimateTokens(handoff.summary)} токенов`
  };
}

/**
 * Помощь по командам KB
 * @returns {string} текст помощи
 */
function getKbHelp() {
  return `Knowledge Base команды:

/kb handoff "описание" [--new-session]
  Сохранить текущую сессию в KB
  --new-session — создать новую сессию после сохранения

/kb handoffs [--limit N] [--search "запрос"]
  Показать handoffs в KB

/kb load <id|номер|частичное_совпадение>
  Загрузить handoff в контекст

/kb help
  Показать эту справку

Примеры:
  /kb handoff "Исследовал архитектуру OpenClaw"
  /kb handoff "Решена проблема с портом 8080" --new-session
  /kb handoffs --limit 10
  /kb handoffs --search "архитектура"
  /kb load 1
  /kb load "архитектура"
`;
}

// ─── Вспомогательные функции для handoff ──────────────────────────────────

/**
 * Получить сообщения сессии для handoff
 * @param {string} sessionId - ID сессии
 * @returns {array} массив сообщений
 */
function getSessionMessagesForHandoff(sessionId) {
  const out = runPython(SESSION, ['get_messages', '--session-id', sessionId], 10000);
  const data = parseJson(out);
  return data?.messages || [];
}

/**
 * Отбор сообщений для handoff (семантический или last N)
 * @param {array} messages - все сообщения сессии
 * @returns {array} отобранные сообщения
 */
function selectMessagesForHandoff(messages) {
  const config = TR_CONFIG.handoff || {};
  const { maxMessages, minMessagesForSemantic, alwaysIncludeLast } = config;
  
  const maxMsg = maxMessages || 20;
  const minSemantic = minMessagesForSemantic || 15;
  const alwaysLast = alwaysIncludeLast || 5;
  
  // Если сообщений мало — вернуть все
  if (messages.length <= maxMsg) {
    return messages;
  }
  
  // Если сообщений достаточно для семантического поиска
  if (messages.length >= minSemantic) {
    // TODO: семантический отбор через pgvector
    // Пока — fallback на last N
    log(`selectMessagesForHandoff: semantic search not implemented yet, using last ${maxMsg}`);
  }
  
  // Fallback: последние N сообщений
  return messages.slice(-maxMsg);
}

/**
 * Форматирование сообщений для handoff
 * @param {array} messages - сообщения
 * @returns {string} форматированный текст
 */
function formatMessagesForHandoff(messages) {
  return messages.map(m => {
    const role = m.role === 'user' ? 'User' : 'Assistant';
    return `[${role}]\n${m.content}`;
  }).join('\n\n');
}

/**
 * Сохранение в KB
 * @param {string} title - заголовок
 * @param {string} content - контент
 * @param {string} sessionId - ID сессии
 * @returns {object} результат (id, etc.)
 */
function saveToKb(title, content, sessionId) {
  // Генерация summary через LLM (упрощённо — пока используем title)
  const summary = title; // TODO: генерация summary через LLM
  
  const out = runPython(KB_STORE, [
    'kb_save',
    '--title', title,
    '--summary', summary,
    '--content', content,
    '--source-tool', 'session-handoff',
    '--category', 'session'
  ], 30000);
  
  return parseJson(out);
}

/**
 * Создание новой сессии
 * @param {string} oldSessionId - ID старой сессии
 */
function createNewSession(oldSessionId) {
  // Архивирование старой сессии (если нужно)
  // Создание новой сессии
  log(`createNewSession: archived old session ${oldSessionId}`);
  // TODO: реализация архивирования и создания новой сессии
}

/**
 * Получение handoffs из KB
 * @param {number} limit - лимит
 * @param {string|null} search - поисковый запрос
 * @returns {array} список handoffs
 */
function getHandoffsFromKb(limit, search) {
  // kb_store.py не имеет kb_list — используем kb_search с category='session'
  const args = ['kb_search', '--query', search || '', '--category', 'session', '--limit', limit.toString()];
  
  const out = runPython(KB_STORE, args, 10000);
  const data = parseJson(out);
  return data?.results || [];
}

/**
 * Поиск handoff по ID/номеру/title
 * @param {string} query - запрос
 * @returns {object|null} handoff или null
 */
function findHandoffById(query) {
  // Вариант 1: номер (#1 или 1)
  if (/^#?\d+$/.test(query)) {
    const index = parseInt(query.replace('#', ''), 10) - 1;
    const handoffs = getHandoffsFromKb(index + 1, null);
    return handoffs[index] || null;
  }
  
  // Вариант 2: частичное совпадение по title
  const handoffs = getHandoffsFromKb(100, query);
  if (handoffs.length > 0) {
    return handoffs[0];
  }
  
  return null;
}

/**
 * Форматирование handoff для контекста
 * @param {object} handoff - handoff объект
 * @returns {string} форматированный текст
 */
function formatHandoffContext(handoff) {
  const date = new Date(handoff.created_at).toISOString().replace('T', ' ').substring(0, 19);
  return block('PREVIOUS SESSION HANDOFF',
    `Title: ${handoff.title}\n` +
    `Date: ${date} UTC\n` +
    `\nSummary:\n${handoff.summary}`
  );
}

/**
 * Оценка токенов (приблизительно)
 * @param {string} text - текст
 * @returns {number} количество токенов
 */
function estimateTokens(text) {
  return Math.round(text.length / 4); // ~4 символа на токен
}

// ─── Конец функций для handoff ────────────────────────────────────────────

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
  
  // ─── Обработка команд KB ────────────────────────────────────────────────
  if (content.startsWith('/kb ')) {
    const result = handleKbCommands(content.substring(4).trim(), sessionId, event);
    if (result) {
      if (result.text) {
        return { text: result.text };
      }
      if (result.skip) {
        return; // Команда обработана, не сохранять в сессию
      }
    }
  }
  // ─── Конец обработки команд KB ──────────────────────────────────────────
  
  runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
  // Сохраняем sessionId + content вместе для последующего pair_write
  pendingUserMessages.set('current', { sessionId, content });
  log(`pending_user: sessionId=${sessionId} saved (${content.length} chars)`);
}

export async function beforePromptBuild(event, ctx) {
  const t0 = Date.now();
  
  if (ctx?.sessionId) lastKnownSessionId = ctx.sessionId;
  const userPrompt = event?.prompt || event?.content || '';
  if (!userPrompt) return {};

  // ─── Перехват команд /kb ДО передачи в LLM ──────────────────────────────
  log(`before_prompt_build: userPrompt="${userPrompt.substring(0, 50)}"`);
  if (userPrompt.startsWith('/kb ')) {
    const sessionId = resolveSessionId(ctx, event);
    const result = handleKbCommands(userPrompt.substring(4).trim(), sessionId, event);
    log(`before_prompt_build: /kb command result=${JSON.stringify(result)}`);
    if (result?.text) {
      // Возвращаем ответ напрямую (bypass LLM)
      log(`before_prompt_build: /kb command handled directly`);
      return { text: result.text };
    }
  }
  // ─── Конец перехвата команд /kb ─────────────────────────────────────────

  const sessionId = resolveSessionId(ctx, event);
  log(`before_prompt_build: sessionKey=${ctx?.sessionKey} sessionId=${ctx?.sessionId} agentId=${ctx?.agentId}`);
  // Сохраняем маппинг channelId:senderId → sessionId для message_received
  if (ctx?.sessionId && ctx?.channelId) {
    const compositeKey = `${ctx.channelId}:gateway-client`;
    if (!sessionMap.has(compositeKey)) {
      sessionMap.set(compositeKey, ctx.sessionId);
    }
  }

  // Стабильный контекст (prependSystemContext) — ВСЁ здесь
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

  // KB — релевантная информация из Knowledge Base
  const kb = getKB(userPrompt);
  if (kb) stableParts.push(block('KNOWLEDGE BASE', kb));

  const totalParts = stableParts.length;
  log(`before_prompt_build: ${Date.now() - t0}ms | parts=${totalParts} session=${sessionId || 'none'}`);

  const result = {};
  if (stableParts.length) {
    result.prependSystemContext = stableParts.join('\n\n');
    
    // DEBUG: переключатель через конфиг → env
    const debugMode = TR_CONFIG.debug ?? (process.env.TOTAL_RECALL_DEBUG === '1');
    if (debugMode) {
      const hasSkeleton = result.prependSystemContext.includes('SESSION SKELETON');
      const hasFocus = result.prependSystemContext.includes('SESSION FOCUS');
      const hasCore = result.prependSystemContext.includes('CORE.md');
      const hasMemory = result.prependSystemContext.includes('MEMORY CONTEXT');
      log(`DEBUG: prependSystemContext contains: CORE=${hasCore}, MEMORY=${hasMemory}, SKELETON=${hasSkeleton}, FOCUS=${hasFocus}`);
      
      // Показать содержимое SESSION SKELETON (первые 500 символов)
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

export function onMessageSent(event, ctx) {
  const agentId = ctx?.agentId || 'main';
  if (agentId !== 'main') return;
  
  // agent_end event: event.messages — массив всех сообщений сессии
  // Берём последнее assistant сообщение
  const lastAssistant = [...event.messages].reverse().find(m => m.role === 'assistant');
  const content = lastAssistant?.content?.find(c => c.type === 'text')?.text;
  
  log(`onMessageSent: sessionId=${ctx?.sessionId} messages=${event?.messages?.length || 0} content=${!!content}`);
  
  // Берём pending из буфера (там sessionId + content)
  const pending = pendingUserMessages.get('current');
  pendingUserMessages.delete('current');
  
  if (pending) {
    // sessionId из ctx (agent_end) — правильный, pending.sessionId может быть устаревшим
    const sessionId = ctx?.sessionId || lastKnownSessionId || pending.sessionId;
    const pendingUser = pending.content;
    
    if (!content || !sessionId) {
      log(`pair_write: skip — content=${!!content} sessionId=${sessionId}`);
      return;
    }
    
    runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
    
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
      log(`message_write: skip — content=${!!content} sessionId=${sessionId}`);
      return;
    }
    
    runPython(SESSION, ['session_start', '--session-id', sessionId], 5000);
    
    log(`message_write: sessionId=${sessionId} assistant=${content.length}`);
    runPython(SESSION, [
      'message_write',
      '--session-id', sessionId,
      '--role', 'assistant',
      '--content', content,
    ], 5000);
  }
}
