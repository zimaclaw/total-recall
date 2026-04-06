# Total Recall — Документация по интеграции

*OpenClaw Plugin. Curator: сборка окна контекста перед каждым ответом Пятницы.*

---

## Архитектура

```
Пользователь пишет промпт
  ↓
message_received (plugin hook)
  → читает sessionId из sessions.json
  → пишет чистый промпт в PostgreSQL (session_messages + embedding)
  ↓
before_prompt_build (plugin hook)
  → собирает окно из 3 источников:
    [1] CORE.md (~2K)           → всегда
    [2] Flashback из Neo4j      → опыт агента по теме
    [3] Скелет сессии           → все промпты пользователя (без ответов)
    [4] Фокус сессии            → семантически релевантный кусок
  → return { prependSystemContext }
  ↓
Пятница видит контекст + отвечает
  ↓
message_sent (plugin hook)
  → пишет ответ ассистента в PostgreSQL
```

---

## Компоненты

### handler.js
Основная логика plugin. Регистрирует хуки через `api.on()`.

**Расположение:** `~/projects/total-recall/handler.js`  
**Деплой:** `~/.openclaw/extensions/total-recall/handler.js`

### index.js
Точка входа plugin. Регистрирует хуки через OpenClaw plugin API.

**Расположение:** `~/projects/total-recall/index.js`

### session_store.py
Python CLI для работы с PostgreSQL session store.

**Расположение:** `~/projects/total-recall/skills/memory-reflect/session_store.py`  
**Рабочая версия:** `~/.openclaw/skills/memory-reflect/session_store.py`  
**Зависимости:** `psycopg2-binary`, `httpx` (в venv memory-reflect)

**Структура memory-reflect:**
```
~/.openclaw/skills/memory-reflect/
├── .venv/                    # Виртуальное окружение poetry
├── pyproject.toml            # Poetry config
├── poetry.lock               # Fixed dependencies
├── .env                      # Переменные окружения (не в git)
├── config.py                 # Конфигурация
├── memory-reflect.py         # CLI: flashback, reflect, status
├── session_store.py          # CLI: PostgreSQL session store
├── kb_store.py               # CLI: Knowledge Base
├── store.py                  # Общие функции
├── migrate_summary.py        # Миграции
└── memory-daemon.py          # Демон для фоновой работы
```

### memory-reflect.py
Существующий CLI для flashback из Neo4j/Qdrant.

**Расположение:** `~/.openclaw/skills/memory-reflect/memory-reflect.py`

---

## Хуки OpenClaw

### Актуальная карта хуков (проверено в исходниках)

| Хук | Тип | event | ctx | Использование |
|---|---|---|---|---|
| `message_received` | void | `{from, content, timestamp, metadata}` | `{channelId, accountId, conversationId}` | Запись промпта в PostgreSQL |
| `before_prompt_build` | modifying | `{prompt, messages}` | `{agentId, sessionKey, sessionId, workspaceDir}` | Сборка окна контекста |
| `message_sent` | void | `{content, ...}` | `{sessionId, ...}` | Запись ответа в PostgreSQL |
| `command:new` | void | `{action}` | `{sessionKey, sessionId}` | Граница сессии |

**Важно:** `before_prompt_build` возвращает `{ prependContext?, prependSystemContext?, systemPrompt?, appendSystemContext? }`.

**Где отображаются:**
- `prependContext` — виден в UI (над сообщением пользователя)
- `prependSystemContext` — не виден в UI (безопасно для системного контекста)
- `systemPrompt` — не виден в UI
- `appendSystemContext` — не виден в UI

**Решение:** SESSION SKELETON и SESSION FOCUS в `prependSystemContext` — не видны в чате.

### Получение sessionId в message_received

В `message_received` `sessionId` недоступен в ctx — он разрешается позже. Решение: читать `sessions.json` напрямую.

```javascript
const storePath = `${homedir()}/.openclaw/agents/main/sessions/sessions.json`;
const store = JSON.parse(readFileSync(storePath, 'utf8'));
// Ищем запись с нужным channelId
for (const [key, entry] of Object.entries(store)) {
  if (entry?.sessionId && key.includes(channelId)) {
    return entry.sessionId;
  }
}
```

**Формат sessions.json:**
```json
{
  "agent:main:main": {
    "sessionId": "0dd9c9f5-77d1-4fb6-9753-80e1fae9db84",
    "lastChannel": "webchat",
    ...
  }
}
```

### allowPromptInjection

**Не существует** как ключ конфига `openclaw.json` — вызывает ошибку валидации.  
`before_prompt_build` работает через plugin API без дополнительных настроек.

---

## PostgreSQL схема

**Хост:** `.145:5432`  
**База:** `openclaw`  
**Пользователь:** `openclaw`

```sql
-- Сессии
CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,        -- sessionId от OpenClaw (UUID)
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Сообщения (чистые промпты без injected контекста)
CREATE TABLE session_messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Векторы для семантического поиска фокуса
CREATE TABLE session_vectors (
    id         BIGSERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL REFERENCES session_messages(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    embedding  vector(1024),            -- bge-m3
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индексы
CREATE INDEX idx_session_messages_user ON session_messages(session_id, ts) WHERE role = 'user';
CREATE INDEX idx_session_vectors_hnsw ON session_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### session_store.py команды

```bash
# Создать/обновить сессию
python session_store.py session_start --session-id <uuid>

# Записать сообщение (user: с embedding, assistant: без)
python session_store.py message_write --session-id <uuid> --role user --content "текст"

# Получить скелет (все промпты пользователя)
python session_store.py skeleton --session-id <uuid> --max-tokens 2000

# Найти фокус по семантике
python session_store.py focus --session-id <uuid> --query "тема" --top-k 5 --min-score 0.4
```

---

## Конфигурация

### .env (memory-reflect)
```bash
# Добавлено для session_store.py
PG_DSN=postgresql://openclaw:<password>@192.168.1.145:5432/openclaw
```

### openclaw.json (фрагмент)
```json
{
  "plugins": {
    "entries": {
      "total-recall": {
        "enabled": true,
        "config": {}
      }
    }
  }
}
```

---

## Ключевые находки (исследование исходников)

### .jsonl файлы сессий
OpenClaw хранит сессии в `~/.openclaw/agents/main/sessions/{sessionId}.jsonl`.  
**Проблема:** user сообщения в `.jsonl` содержат injected контекст — они загрязнены нашим `prependSystemContext`. Читать скелет из `.jsonl` нельзя. Поэтому нужна PostgreSQL как отдельное хранилище чистых промптов.

### curator hook (удалён)
Старый файловый hook `~/.openclaw/hooks/curator/` конфликтовал с plugin.  
Удалён: `rm -rf ~/.openclaw/hooks/curator`

### before_agent_start (legacy)
Старый хук оставлен для совместимости. `before_prompt_build` — правильный хук для инжекции контекста.

### agent_end (используется для pair_write)
Срабатывает один раз за turn для TUI/webchat.
- event.messages содержит полный snapshot сообщений
- ctx.sessionId доступен напрямую
- Идеально для post-turn обработки (pair_write, reflexion)

**История попыток:**
- `message:sent` (файловый хук) → не срабатывает для TUI/webchat
- `message_sent` через api.on() → множественные вызовы на tool call'ы
- `before_message_write` → не срабатывает
- `agent_end` → работает идеально ✅

---

## Диагностика

```bash
# Лог plugin
tail -f /tmp/total-recall.log

# Лог gateway
openclaw logs --follow

# Проверить сессии в PostgreSQL
PGPASSWORD=<pw> psql -h 192.168.1.145 -U openclaw -d openclaw \
  -c "SELECT id, started_at FROM sessions ORDER BY started_at DESC LIMIT 5;"

# Проверить сообщения
PGPASSWORD=<pw> psql -h 192.168.1.145 -U openclaw -d openclaw \
  -c "SELECT session_id, role, LEFT(content,80) FROM session_messages ORDER BY id DESC LIMIT 10;"
```
