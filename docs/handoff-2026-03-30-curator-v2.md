# Handoff — сессия 2026-03-30 (Curator v2)

## Что сделано сегодня

### PostgreSQL — session store
- Создан пользователь `openclaw`, база `openclaw`, расширение `pgvector`
- Схема: `sessions`, `session_messages`, `session_vectors` с HNSW индексом
- `session_store.py` — CLI с 4 командами: `session_start`, `message_write`, `skeleton`, `focus`
- Зависимости добавлены в venv memory-reflect: `psycopg2-binary`, `httpx`
- Конфиг `PG_DSN` добавлен в `.env` и `config.py`

### total-recall plugin — Curator v2
- `handler.js` переписан полностью
- Хуки: `command:new`, `message_received`, `before_prompt_build`, `message_sent`
- `before_prompt_build` (не `before_agent_start`) — правильный хук для инжекции
- `parts=3` — CORE.md + flashback + скелет + фокус работают
- `allowPromptInjection` — не существует, убран из конфига

### Решена проблема sessionId в message_received
- `sessionId` в `message_received` недоступен через ctx
- Решение: читать `~/.openclaw/agents/main/sessions/sessions.json` напрямую
- sessions.json: `{ "agent:main:main": { "sessionId": "uuid" } }`

### Исследования (в docs/)
- `session-id-research.md` — почему sessionId недоступен, варианты решения
- `session-store-research.md` — где хранятся сессии, почему нельзя читать .jsonl

### Удалено
- `~/.openclaw/hooks/curator/` — старый файловый hook, конфликтовал с plugin

### Документация (в docs/)
- `integration.md` — полная документация по интеграции plugin с OpenClaw
- `decisions.md` — зафиксированные архитектурные решения и отказы

---

## Текущее состояние системы

| Компонент | Адрес | Статус |
|---|---|---|
| Пятница (Qwen3.5-27B) | llamacpp :8180 на .181 | ✅ работает |
| ollama основной | .145:11434 | qwen3:14b — заменяем |
| ollama mem0 | .145:11435 | bge-m3 ✅, qwen3:4b-mem0 — убираем |
| Qdrant | .145:6333 | reflections удалена, memories закрыта |
| Neo4j | .145:7687 | Task→Conclusion→Lesson→Principle→Meta ✅ |
| PostgreSQL | .145:5432 | sessions + session_messages + session_vectors ✅ |
| OpenClaw gateway | машина 3 | total-recall v0.2.0 ✅ |

### Что работает прямо сейчас
- Flashback из Neo4j по категории промпта ✅
- Запись чистых промптов в PostgreSQL ✅
- Скелет сессии (все user промпты) ✅
- Фокус сессии (pgvector поиск) ✅
- CORE.md инжекция ✅
- parts=3 в before_prompt_build ✅

---

## Следующие шаги (приоритет)

### Приоритет 1 — Проверка качества скелета и фокуса

Написать несколько сообщений в новой сессии, потом проверить:
```bash
PGPASSWORD=<pw> psql -h 192.168.1.145 -U openclaw -d openclaw \
  -c "SELECT role, LEFT(content,100) FROM session_messages ORDER BY id;"
```

Убедиться что:
- Контент чистый (без `=== MEMORY CONTEXT ===`)
- Фокус находит релевантные куски
- Время ответа приемлемое (сейчас ~3-4s на before_prompt_build)

### Приоритет 2 — Архивирование прошлых сессий

При `/new` синхронизировать закрытую сессию в PostgreSQL из `.jsonl`.

**Механика:**
- Хук `command:new` уже есть в plugin
- Добавить: читать `{sessionId}.jsonl.reset.*` файл
- Парсить только `type=message` записи
- Фильтровать injected контекст (строки начинающиеся с `=== `)
- Писать в PostgreSQL

**Зачем:** OpenClaw удаляет старые `.jsonl`. PostgreSQL — долгосрочный архив.

### Приоритет 3 — Neo4j vector index

```cypher
CREATE VECTOR INDEX conclusion_embedding IF NOT EXISTS
FOR (c:Conclusion) ON (c.embedding)
OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } }
```

Перегенерировать embeddings: формат "запрос → outcome" вместо текста эпизода.  
Удалить вызовы к Qdrant reflections в memory-reflect.py.

### Приоритет 4 — Оптимизация latency

Сейчас `before_prompt_build` занимает ~3-4s. Причина: последовательные вызовы Python.

Варианты:
- Параллельные вызовы (Promise.all для skeleton + focus + flashback)
- Кэш flashback — результат не меняется если категория та же
- HTTP API вместо subprocess для session_store

### Приоритет 5 — KB (Knowledge Base)

Создать таблицы `kb_hot` + `kb_cold` в PostgreSQL.  
Инструменты агента: `kb_save()`, `kb_promote()`, `kb_fetch()`.  
Qdrant коллекция `knowledge_base` для векторного поиска KB.

### Фоновые (не горят)
- Qwen3.5-27B на .145 как агент памяти (слот 1) — после стабилизации
- Bootstrap Game формализация — уровень 3 засчитать Пятнице?
- Watchdog — архитектура есть, реализации нет
- CORE.md — кто обновляет и когда?

---

## Открытые архитектурные вопросы

1. **Архивирование сессий:** как фильтровать injected контекст при парсинге `.jsonl`? Можно ли надёжно определить границу между чистым промптом и нашим prependContext?

2. **Граница сессии для Telegram:** `channelId=webchat` для TUI, но что для Telegram? Нужно ли разделять сессии по каналу?

3. **CORE.md:** кто обновляет — Пятница сама через tool call или агент памяти по триггеру?

4. **KB:** кто решает сохранять — агент сам или автоматически после каждого web_search?

5. **Latency:** 3-4s overhead — приемлемо? Если нет — что оптимизировать первым?

---

## Команды для проверки состояния

```bash
# Plugin лог
tail -20 /tmp/total-recall.log

# Gateway лог
openclaw logs --follow

# PostgreSQL
PGPASSWORD=<pw> psql -h 192.168.1.145 -U openclaw -d openclaw \
  -c "SELECT id, started_at, last_at FROM sessions ORDER BY started_at DESC LIMIT 5;"

# Memory stack
cd ~/.openclaw/skills/memory-reflect
.venv/bin/python memory-reflect.py --status
.venv/bin/python memory-reflect.py --flashback --category infra

# Демон рефлексии
sudo systemctl status total-recall-daemon

# Plugin status
openclaw hooks list | grep -v total-recall
```

---

## Наработки для следующей сессии

### Код архивирования сессии (заготовка)

```javascript
// В onCommandNew — после создания новой сессии
// Архивировать предыдущую сессию из .jsonl в PostgreSQL
async function archivePreviousSession(prevSessionId, agentId = 'main') {
  const { homedir } = require('node:os');
  const { readFileSync, readdirSync } = require('node:fs');
  const sessionsDir = `${homedir()}/.openclaw/agents/${agentId}/sessions`;
  
  // Ищем .jsonl.reset.* файл предыдущей сессии
  const files = readdirSync(sessionsDir);
  const resetFile = files.find(f => f.startsWith(prevSessionId) && f.includes('.reset.'));
  if (!resetFile) return;
  
  const lines = readFileSync(`${sessionsDir}/${resetFile}`, 'utf8').split('\n');
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line);
      if (entry.type !== 'message') continue;
      const role = entry.message.role;
      if (role !== 'user' && role !== 'assistant') continue;
      
      // Извлечь текст
      const content = Array.isArray(entry.message.content)
        ? entry.message.content.find(c => c.type === 'text')?.text || ''
        : entry.message.content || '';
      
      // Фильтровать injected контекст
      if (content.startsWith('=== ')) continue;
      const cleanContent = content.replace(/^=== .*?=== END .*? ===\n\n/gs, '').trim();
      if (!cleanContent) continue;
      
      // Писать в PostgreSQL
      runPython(SESSION, ['message_write',
        '--session-id', prevSessionId,
        '--role', role,
        '--content', cleanContent,
      ], 15000);
    } catch(e) {}
  }
}
```

### SQL для проверки качества данных

```sql
-- Посмотреть скелет сессии
SELECT ts, LEFT(content, 200) 
FROM session_messages 
WHERE session_id = '0dd9c9f5-77d1-4fb6-9753-80e1fae9db84' 
  AND role = 'user'
ORDER BY ts;

-- Проверить embeddings
SELECT COUNT(*) FROM session_vectors WHERE session_id = '0dd9c9f5-77d1-4fb6-9753-80e1fae9db84';
```
