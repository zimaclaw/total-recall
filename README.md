# Total Recall — OpenClaw Plugin

Curator v2: сборка окна контекста перед каждым ответом Пятницы + memory-reflect для долгосрочной памяти.

## Что делает

### Curator (before_prompt_build hook)
Перед каждым запуском агента:
1. Читает CORE.md (~2K токенов)
2. Запускает `memory-reflect.py --flashback --category <category>` для получения опыта
3. Собирает SESSION SKELETON (все промпты пользователя из текущей сессии)
4. Собирает SESSION FOCUS (семантически релевантный кусок сессии)
5. Inject всё в `prependSystemContext` (не видно в UI)

### Session Store (message_received + agent_end hooks)
1. Записывает чистые промпты в PostgreSQL (без injected контекста)
2. Записывает пары user+assistant для скелета сессии
3. Создаёт pgvector embeddings для семантического поиска фокуса

## Структура

```
total-recall/
├── package.json                     # Метаданные npm + openclaw extensions
├── index.js                         # Регистрация хуков (message_received, before_prompt_build, agent_end)
├── handler.js                       # Логика: Curator + session store
├── openclaw.plugin.json             # UI hints и config schema
├── README.md                        # Эта документация
├── docs/                            # Документация
│   ├── architecture.md              # Архитектура системы
│   ├── integration.md               # Интеграция с OpenClaw
│   ├── decisions.md                 # Архитектурные решения
│   ├── memory-schema.md             # Схема памяти
│   └── ...                          # Отчёты и исследования
└── skills/                          # Python компоненты
    └── memory-reflect/              # Poetry проект для долгосрочной памяти
        ├── pyproject.toml           # Poetry config
        ├── poetry.lock              # Fixed dependencies
        ├── .env                     # Переменные окружения (не в git)
        ├── config.py                # Конфигурация
        ├── memory-reflect.py        # CLI: flashback, reflect, status
        ├── session_store.py         # CLI: PostgreSQL session store
        ├── kb_store.py              # CLI: Knowledge Base
        ├── store.py                 # Общие функции
        ├── migrate_summary.py       # Миграции
        ├── memory-daemon.py         # Демон для фоновой работы
        └── .venv/                   # Виртуальное окружение (не в git)
```

## Установка

### 1. Копировать в extensions

```bash
cp -r ~/projects/total-recall /home/ironman/.openclaw/extensions/
```

### 2. Добавить в openclaw.json

```json
{
  "plugins": {
    "allow": ["total-recall"],
    "entries": {
      "total-recall": {
        "enabled": true,
        "config": {
          "debug": true,
          "paths": {
            "log": "/tmp/total-recall.log",
            "memoryReflect": "/home/ironman/.openclaw/skills/memory-reflect",
            "coreMd": "/home/ironman/.openclaw/workspace/CORE.md"
          },
          "kb": {
            "scoreThreshold": 0.55,
            "maxResults": 3
          },
          "curator": {
            "defaultContext": 32000
          },
          "embeddings": {
            "url": "http://192.168.1.145:11435",
            "model": "bge-m3",
            "timeout": 30000
          },
          "llm": {
            "provider": "ollama",
            "url": "http://192.168.1.145:11434",
            "model": "qwen3.5:27b-q4_K_M-N2",
            "timeout": 60000
          },
          "postgresql": {
            "host": "192.168.1.145",
            "port": 5432,
            "database": "openclaw"
          },
          "handoff": {
            "maxMessages": 20,
            "similarityThreshold": 0.3,
            "minMessagesForSemantic": 15,
            "alwaysIncludeLast": 5
          }
        }
      }
    }
  }
}
```

### 3. Синхронизировать memory-reflect

```bash
cp -r ~/projects/total-recall/skills/memory-reflect/* ~/.openclaw/skills/memory-reflect/
```

### 4. Перезапустить gateway

```bash
openclaw gateway restart
```

## Конфигурация

Настройки total-recall хранятся в `~/.openclaw/openclaw.json` в секции `plugins.entries["total-recall"].config`.

### Секции конфигурации

#### debug
- **Тип:** boolean
- **По умолчанию:** false
- **Описание:** Включить debug logging

#### paths
- **Тип:** object
- **Поля:**
  - `log` (string): Путь к лог файлу (default: `/tmp/total-recall.log`)
  - `memoryReflect` (string): Путь к memory-reflect skill (default: `/home/ironman/.openclaw/skills/memory-reflect`)
  - `coreMd` (string): Путь к CORE.md (default: `/home/ironman/.openclaw/workspace/CORE.md`)

#### kb
- **Тип:** object
- **Поля:**
  - `scoreThreshold` (number): Порог Similarity для фильтрации результатов KB (default: 0.55)
  - `maxResults` (integer): Максимум результатов из KB (default: 3)

#### curator
- **Тип:** object
- **Поля:**
  - `defaultContext` (integer): Контекстное окно по умолчанию для Curator (default: 32000)

#### embeddings
- **Тип:** object
- **Поля:**
  - `url` (string): URL сервиса embeddings (default: `http://192.168.1.145:11435`)
  - `model` (string): Модель embeddings (default: `bge-m3`)
  - `timeout` (integer): Таймаут в мс (default: 30000)

#### llm
- **Тип:** object
- **Поля:**
  - `provider` (string): Провайдер LLM (default: `ollama`)
  - `url` (string): URL LLM (default: `http://192.168.1.145:11434`)
  - `model` (string): Модель LLM (default: `qwen3.5:27b-q4_K_M-N2`)
  - `timeout` (integer): Таймаут в мс (default: 60000)

#### postgresql
- **Тип:** object
- **Поля:**
  - `host` (string): Хост PostgreSQL (default: `192.168.1.145`)
  - `port` (integer): Порт PostgreSQL (default: 5432)
  - `database` (string): База данных (default: `openclaw`)

#### handoff
- **Тип:** object
- **Поля:**
  - `maxMessages` (integer): Максимум сообщений для handoff (default: 20)
  - `similarityThreshold` (number): Порог Similarity для семантического поиска (default: 0.3)
  - `minMessagesForSemantic` (integer): Минимум сообщений для включения семантического поиска (default: 15)
  - `alwaysIncludeLast` (integer): Всегда включать последние N сообщений (default: 5)

### Fallback

Если секция в конфиге отсутствует — используется fallback:
1. Конфиг (openclaw.json)
2. Переменные окружения (env)
3. Хардкод значения (hardcode)

Пример:
```javascript
const LOG = TR_CONFIG.paths?.log || '/tmp/total-recall.log';
const DEBUG_MODE = TR_CONFIG.debug ?? (process.env.TOTAL_RECALL_DEBUG === '1');
```

---

## Проверка

### Лог plugin

```bash
tail -f /tmp/total-recall.log
```

### Лог memory-reflect

```bash
cd ~/.openclaw/skills/memory-reflect
.venv/bin/python memory-reflect.py --status
```

### Проверить PostgreSQL

```bash
PGPASSWORD=<pw> psql -h 192.168.1.145 -U openclaw -d openclaw \
  -c "SELECT id, started_at FROM sessions ORDER BY started_at DESC LIMIT 5;"
```

## Хуки OpenClaw

| Хук | Тип | Использование |
|-----|-----|---------------|
| `message_received` | void | Запись промпта в PostgreSQL |
| `before_prompt_build` | modifying | Сборка окна контекста (Curator) |
| `agent_end` | void | Запись пары user+assistant (pair_write) — **только main agent** |
| `command:new` | void | Граница сессии |

**Важно:** `onMessageSent` обрабатывает только сообщения от main agent (`agentId === 'main'`), игнорируя субагентов.

## Контекст (prependSystemContext)

```
=== CORE.md ===
[~2K токенов: Stack, Active Tasks, Principles]
=== END CORE.md ===

=== MEMORY CONTEXT ===
[flashback из Neo4j: уроки и принципы по категории]
=== END MEMORY CONTEXT ===

=== SESSION SKELETON ===
[все промпты пользователя из текущей сессии]
=== END SESSION SKELETON ===

=== SESSION FOCUS ===
[семантически релевантный кусок сессии]
=== END SESSION FOCUS ===
```

**Важно:** `prependSystemContext` не виден в UI — только используется моделью.

## DEBUG режим

Включение:
```bash
export TOTAL_RECALL_DEBUG=1
openclaw gateway restart
```

Отключение:
```bash
unset TOTAL_RECALL_DEBUG
openclaw gateway restart
```

Лог: `/tmp/total-recall.log`

Показывает:
- Содержимое prependSystemContext (CORE, MEMORY, SKELETON, FOCUS)
- Содержимое SESSION SKELETON (первые 500 символов)

## Логика категоризации

Категории определяются по ключевым словам в prompt:

- **infra**: deploy, server, docker, nginx, port, network, сервер, порт, деплой, systemd
- **dev**: code, script, bug, fix, function, git, код, скрипт, баг, python, node
- **memory**: memory, remember, flashback, reflect, память, вспомни, принцип
- **research**: research, find, search, analyze, исследуй, найди, поищи
- **test**: test, check, validate, verify, тест, проверь
- **deploy**: deploy, release, publish, деплой, релиз
- **plan**: plan, schedule, roadmap, план, роадмап
- **write**: write, document, report, напиши, документ

Если ключевые слова не найдены — категория по умолчанию `dev`.

## Memory-reflect CLI

```bash
cd ~/.openclaw/skills/memory-reflect

# Статус рефлексии
.venv/bin/python memory-reflect.py --status

# Flashback по категории
.venv/bin/python memory-reflect.py --flashback --category infra

# Flashback по теме (семантический)
.venv/bin/python memory-reflect.py --flashback --focus "docker port conflict"

# Ручная рефлексия
.venv/bin/python memory-reflect.py --reflect

# Комбо — тема внутри категории
.venv/bin/python memory-reflect.py --flashback --focus "тема" --category infra
```

## Session Store CLI

```bash
cd ~/.openclaw/skills/memory-reflect

# Создать/обновить сессию
.venv/bin/python session_store.py session_start --session-id <uuid>

# Записать сообщение
.venv/bin/python session_store.py message_write --session-id <uuid> --role user --content "текст"

# Получить скелет
.venv/bin/python session_store.py skeleton --session-id <uuid> --max-tokens 2000

# Найти фокус
.venv/bin/python session_store.py focus --session-id <uuid> --query "тема" --top-k 5 --min-score 0.4
```

## Knowledge Base (kb_save)

Сохранение важной информации в Knowledge Base для повторного использования.

**Когда использовать:** После tavily/web_search с полезной информацией — сохрани результат в KB чтобы не искать повторно.

```bash
cd ~/.openclaw/skills/memory-reflect

# Сохранить в KB
.venv/bin/python kb_store.py kb_save \
  --title "заголовок" \
  --summary "краткое описание (~200 токенов)" \
  --content "полный текст" \
  --source-url "url" \
  --source-tool "tavily" \
  --category "search"

# Поиск в KB
.venv/bin/python kb_store.py kb_search \
  --query "запрос" \
  --limit 3

# Промотировать в cold (если запись оказалась полезной)
.venv/bin/python kb_store.py kb_promote --id <uuid>
```

**Параметры kb_save:**
- `--title` — заголовок записи (обязательно)
- `--summary` — краткое описание ~200 токенов (обязательно)
- `--content` — полный текст (обязательно)
- `--source-url` — URL источника (опционально)
- `--source-tool` — инструмент откуда пришла информация (опционально, default: "tavily")
- `--category` — категория (опционально, default: "search")

---

## Handoff (сохранение сессии в KB)

**Статус:** ⏳ в реализации (task-17, 2026-04-22)

**Цель:** Ручное сохранение текущей сессии в Knowledge Base для восстановления контекста позже.

**Команды:**

```bash
# Создать handoff
/kb handoff "Описание результата или задачи"

# Создать handoff и начать новую сессию
/kb handoff --new-session "Завершена настройка"

# Показать существующие handoffs (будет реализовано)
/kb handoffs

# Загрузить handoff (будет реализовано)
/kb load <id>
```

**Как работает:**
1. Собирает последние 20 сообщений сессии из PostgreSQL
2. Генерирует summary через LLM (~200 токенов)
3. Сохраняет в KB через `kb_store.py kb_save --source-tool handoff`
4. Создаёт mapping в таблице `kb_session_mapping`

**Файл плана:** `PLAN_PHASE1_HANDOFF.md`

---

## PostgreSQL схема

**Хост:** `.145:5432`  
**База:** `openclaw`  
**Пользователь:** `openclaw`

```sql
-- Сессии
CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,
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

-- Векторы для семантического поиска
CREATE TABLE session_vectors (
    id         BIGSERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL REFERENCES session_messages(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    embedding  vector(1024),
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

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

## Известные проблемы

1. **sessionId в message_received** — недоступен в ctx, читаем из sessions.json
2. **prependContext виден в UI** — используем prependSystemContext вместо prependContext
3. **memory-reflect использует python3** — не python (строка 377 в session_store.py)

## Версия

v0.2.1 (2026-04-18) — добавлен фильтр agentId в onMessageSent, добавлена документация kb_save
