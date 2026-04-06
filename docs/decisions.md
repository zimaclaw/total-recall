# Архитектурные решения — OpenClaw Personal Assistant

*Зафиксированные решения и отказы. Обновлено: 2026-04-06*

---

## Словарь (зафиксирован)

| Термин | Определение |
|---|---|
| **Опыт** | То что агент заработал через действия. Task→Conclusion→Lesson→Principle→Meta |
| **Flashback** | Механизм извлечения опыта перед действием через Curator |
| **Скелет сессии** | Все промпты пользователя из текущей сессии (без ответов и без injected контекста) |
| **Фокус сессии** | Семантически релевантный кусок сессии по текущему промпту |
| **KB** | То что агент нашёл снаружи: результаты поиска, API, документы |
| **CORE.md** | ~2K блок всегда в system prompt. Редактируется агентом |

---

## Хранилища — финальная архитектура

### Neo4j 5.26.4 · .145:7687
Опыт агента. Вечное хранилище.
```
Task → Evidence → Conclusion → Lesson → Principle → Meta
```
- VECTOR INDEX ON Conclusion(embedding) — **реализовано 2026-03-31**
- Контекстный embedding: "запрос → outcome"
- Один Cypher запрос: vector search + граф обход вверх

### PostgreSQL + pgvector · .145:5432
Сессии + KB. Долгосрочное хранилище.
```
sessions          — одна строка на сессию
session_messages  — чистые промпты (без injected контекста)
session_vectors   — pgvector embeddings для фокуса
session_pairs     — пары user+assistant для скелета
kb_hot            — найденное агентом, 7 дней (не реализовано)
kb_cold           — промотированное, вечное (не реализовано)
```

### Qdrant · .145:6333
Только KB векторы (когда KB будет реализовано).
- Коллекция `reflections` — **удалена** (заменено Neo4j vectors)
- Коллекция `memories` (mem0) — **закрыта** (не мигрировать)

### CORE.md · файл на диске
~2K. Всегда в system prompt. Редактируется агентом через tool call.
**Статус:** файл создан, содержимое добавлено.

---

## Что удалено / закрыто

### mem0 — отказ
**Причина:** recall не работал, данные устарели, архитектура заменена.  
**Что сделано:** коллекция `memories` в Qdrant закрыта без миграции.  
**Модель `qwen3:4b-mem0`** — убрана из стека.

### Qdrant reflections — удалена
**Причина:** заменено Neo4j нативными векторами.  
**Что сделано:** коллекция `reflections` удалена.

### qwen3:14b reflect_model — заменён

**Причина:** Qwen3.5-27B умнее, зоопарк моделей не нужен.  
**Что сделано:** заменён на `qwen3.5:27b-q4_K_M-N2` через Ollama на .145.

**Текущий сетап:**
- Модель: `qwen3.5:27b-q4_K_M-N2`
- Контекст: 40K токенов
- `ollama_num_parallel = 2` — два параллельных контекста
- Третий контекст (слот 3) — памяти скорее всего не хватит, требует тестирования

**Статус:** используется для рефлексии и субагентов. Слот 3 — под вопросом.

### curator hook (файловый) — удалён
**Причина:** конфликтовал с total-recall plugin, дублировал логику.  
**Что сделано:** `rm -rf ~/.openclaw/hooks/curator`

### allowPromptInjection в openclaw.json — не существует
**Причина:** ключ вызывает ошибку валидации конфига.  
**Решение:** `before_prompt_build` работает через plugin API без доп. настроек.

### message_write из onMessageReceived — удалён
**Причина:** дублировал запись user сообщений — они пишутся через pair_write в onMessageSent.  
**Что сделано:** убран вызов message_write из onMessageReceived, оставлен только session_start и pendingUserMessages.set().

### total-recall-message-sent hook (файловый) — заморожен
**Причина:** файловые хуки в ~/.openclaw/hooks/ требуют package.json + правильную структуру.
hook был починен (добавлен package.json, ES modules), но message:sent не срабатывает для TUI/webchat.
**Решение:** pair_write перенесён в extensions/total-recall через api.on('message_sent').
**Статус:** hooks/total-recall-message-sent/ оставлен для истории, не используется.

---

## Топология железа

```
.181 (24 ГБ)                    .145 (28 ГБ, 4 GPU)
─────────────────               ────────────────────────────
Пятница · llamacpp :8180        Qwen3.5-27B · ollama
контекст 165K                   num_parallel=3 · kv=q8_0
основной gateway                Слот 1: агент памяти (планируется)
                                Слот 2: Curator / подсознание (планируется)
                                Слот 3: резерв

                                Neo4j :7687
                                PostgreSQL :5432
                                Qdrant :6333
                                bge-m3 :11435

Машина 3 (Core i7, 8GB)
OpenClaw gateway :8080
total-recall plugin v0.2.0
```

---

## OpenClaw upgrade — 2026.3.1 → 2026.3.22

**Дата:** 2026-04-04  
**Причина:** prependSystemContext (3.7), ContextEngine (3.7).

**Breaking changes которые нас коснулись:**
- Plugin SDK: `openclaw/extension-api` удалён → total-recall не использовал, не задело
- `gateway.auth.mode` обязателен → у нас только `token`, не задело
- `CLAWDBOT_*` / `MOLTBOT_*` env names — не использовали

**Что сделано при upgrade:**
- `openclaw gateway install --force` — пересоздан service config
- Субагенты отфильтрованы: добавлен `if (agentId !== 'main') return` в onMessageReceived и onAgentEnd
- Весь контекст Curator переведён с `prependContext` на `prependSystemContext`
- `compat.supportsUsageInStreaming: true` добавлен в конфиг модели — токены снова отображаются

**Известные баги 2026.4.1 (почему не обновлялись дальше):**
- #59598: timeout для llamacpp embedded run — критично для нас
- #57860: auth regression для custom openai-completions провайдеров

---

## Curator — сборка окна (текущая реализация)

```
prependSystemContext (всё в system prompt):
[1] CORE.md        ~2K    стабильный
[2] Flashback      4-8K   Neo4j: опыт по теме запроса (иерархический)
[3] Скелет         2-4K   PostgreSQL: промпты пользователя текущей сессии
[4] Фокус          4-8K   pgvector: семантически релевантный кусок сессии
[5] KB             2-4K   Qdrant: knowledge base (не реализовано)
```

**Изменение от 2026-03-30:** всё переведено с `prependContext` на `prependSystemContext` — контекст в system prompt, не виден в чате, не копится в истории.

**Порядок:** важное в начало. CORE.md первым. Текущий промпт последним (OpenClaw добавляет сам).

---

## Граница сессии

**Решение:** явный сигнал от пользователя — команда `/new`.

**Почему не таймаут:** непредсказуемо, пользователь лучше знает когда начинается новый контекст.

**Реализация:** хук `command:new` создаёт новую запись в `sessions` + запускает архивирование предыдущей сессии из .jsonl в PostgreSQL (реализовано 2026-04-05).

---

## sessionId в message_received — финальное решение (2026-04-06)

**Проблема:** в `message_received` нет `ctx.sessionId`. При чтении sessions.json находилась не та TUI сессия — первая по порядку, а не текущая активная.

**Симптом:** `message_received` писал sessionId=`b7374efa` (main сессия), `before_prompt_build` использовал `34292fe4` (текущая TUI). pair_write падал с ForeignKeyViolation.

**Решение:** хранить sessionId вместе с content в pendingUserMessages:
```javascript
// В onMessageReceived:
pendingUserMessages.set('current', { sessionId, content });

// В onMessageSent:
const pending = pendingUserMessages.get('current');
pendingUserMessages.delete('current');
const sessionId = lastKnownSessionId || pending.sessionId;
```

**lastKnownSessionId** — глобальная переменная, обновляется в `beforePromptBuild` при каждом вызове. К моменту onMessageSent она уже содержит правильный sessionId текущей TUI сессии.

**Почему не читать sessions.json:** там может быть несколько tui- записей. Первая найденная — не обязательно текущая.

---

## pair_write — хук (2026-04-06)

**Цель:** записывать пары user+assistant в PostgreSQL для sliding context window.

**История попыток:**
- `message:sent` (файловый хук в hooks/) → не срабатывает для TUI/webchat
- `message_sent` через `api.on()` → срабатывает, но множественные вызовы на tool call'ы
- `before_message_write` через `api.on()` → не срабатывает

**Финальное решение:** `agent_end` через `api.on()`
- Срабатывает один раз за turn для TUI/webchat
- event.messages содержит полный snapshot сообщений
- content берётся из последнего assistant сообщения в event.messages
- sessionId берётся из ctx.sessionId (доступен в agent_end напрямую)

**sessionId:** lastKnownSessionId (обновляется в beforePromptBuild) используется как приоритет над pending.sessionId

**Статус:** работает. ✅

---

## afterTurn — ContextEngine, не хук (2026-04-06)

`afterTurn()` — метод интерфейса ContextEngine (`api.registerContextEngine()`), не обычный хук `api.on()`. Доступен только при реализации полного ContextEngine — эксклюзивный слот, один активный на систему.

**Для pair_write afterTurn избыточен** — нужен только хук после каждого turn.

**Если в будущем понадобится ContextEngine:**
- Реализовать интерфейс: `bootstrap`, `ingest`, `assemble`, `compact`, `afterTurn`
- `afterTurn` получает полный snapshot messages + sessionId
- Документация: https://docs.openclaw.ai/concepts/context-engine
- Пример реализации: OpenViking plugin (volcengine/OpenViking на GitHub)
- `afterTurn` — идеальное место для pair_write если перейдём на ContextEngine

---

## Прошлые сессии — архивирование (реализовано 2026-04-05)

**Решение:** `archive_sessions_from_jsonl` в session_store.py.  
**Триггер:** при `/new` через `command:new` хук (асинхронно, не блокирует).

**Что делает:**
1. Читает все `*.jsonl` и `*.jsonl.reset.*` файлы из `~/.openclaw/agents/main/sessions/`
2. Пропускает `*.jsonl.deleted.*`
3. Фильтрует injected контекст через regex: `=== .+? ===[\s\S]*?=== END .+? ===`
4. Пропускает сессии уже в базе (SELECT COUNT FROM sessions)
5. Пишет в PostgreSQL через `message_write`

**Результат первого запуска:** 1,754 сообщения из 28 сессий.

---

## Neo4j vector index — статус

**Реализовано:** 2026-03-31  
**Индекс:** `VECTOR INDEX conclusion_embedding ON Conclusion(embedding)` (dimensions: 1024, cosine)

### Пороги и лимиты flashback

| Уровень | Порог | Лимит |
|---------|-------|-------|
| Conclusion | similarity > 0.65 | топ-5 |
| Lesson | conf > 0.60 | топ-2 |
| Principle | conf > 0.70 | топ-1 |
| Meta | макс similarity > 0.80 | топ-1 |

### Формат embedding

| Уровень | Формат |
|---------|--------|
| Conclusion | `"goal: {goal} \| outcome: {outcome} \| insight: {insight}"` |
| Lesson | `"lesson: {principle} \| mastery: {mastery}"` |
| Principle | `"principle: {statement} \| category: {category}"` |
| Meta | `"meta: {statement}"` |

---

## KB — статус (обновлено 2026-04-06)

### Что реализовано

- **PostgreSQL schema:** kb_hot + kb_cold таблицы с индексами ✅
- **Qdrant collection:** knowledge_base (vector size 1024, Cosine distance) ✅
- **kb_store.py CLI:**
  - kb_save — сохранение записей в KB
  - kb_promote — перемещение из hot в cold
  - kb_fetch — получение по ID
  - kb_search — векторный поиск
  - kb_cleanup — удаление истёкших записей
- **Curator интеграция:** KB как 5-й источник контекста (score > 0.55) ✅
- **Триггер подсказки:** web_search ≥ 750 токенов → KB HINT в следующем контексте ✅

### Архитектура

```
User Query → beforePromptBuild → getKB(prompt) → kb_search → Qdrant → Top 3 results (score > 0.55) → KNOWLEDGE BASE block
```

### Триггер подсказки

```javascript
onAfterToolCall(event, ctx) {
  const isSearch = toolName.includes('search') || toolName.includes('web');
  const tokens = Math.ceil(result.length / 4);
  if (isSearch && tokens >= 750) {
    pendingKbHint = { tool: toolName, tokens };
  }
}
```

### Следующие шаги

- [ ] onAfterToolCall проверить живёт ли в реальном использовании
- [ ] Добавить метрики: сколько раз KB HINT приводил к сохранению
- [ ] Настроить авто-промоушн записей с высоким access_count

---

## CORE.md — постоянный блок памяти

Файл на диске, ~2K токенов. Подаётся в `prependSystemContext` в `before_prompt_build`.

**Содержит:** активные задачи, топ-принципы, адреса стека, договорённости.

**Кто обновляет:** открытый вопрос. Варианты:
- Пятница через tool call
- Агент памяти по триггеру (Principle conf > 0.8)

**Статус:** файл создан и наполнен базовой информацией о стеке.

---

## Субагенты — фильтрация (2026-04-04)

**Проблема:** субагентские сессии пытались писать в PostgreSQL через `message_received` → ForeignKeyViolation.

**Решение:** фильтр по `agentId` в handler.js:
```javascript
const agentId = ctx?.agentId || 'main';
if (agentId !== 'main') return;
```

**Почему:** субагенты — инструменты. Их внутренний диалог не нужен в SESSION SKELETON/FOCUS Пятницы.

---

## Тестирование PLUGIN_PROMPT_MUTATION_RESULT_FIELDS — 2026-04-06 14:28-16:17 UTC

**Цель:** эмпирически проверить где отображаются 4 поля before_prompt_build hook

**Метод:** добавление уникальных тестовых маркеров к каждому полю

### Результаты

| Поле | Статус | Где видно | Вывод |
|------|--------|-----------|-------|
| **prependContext** | ✅ виден | В чате, над сообщением пользователя, серый блок | Добавляется к user prompt, отображается в UI |
| **prependSystemContext** | ❌ не виден | Не отображается в UI | Добавляется к system prompt, НЕ виден в чате |
| **systemPrompt** | ❌ не виден | Не отображается в UI | Полная замена system prompt, НЕ виден в чате |
| **appendSystemContext** | ❌ не виден | Не отображается в UI | Добавляется в конец system prompt, НЕ виден в чате |

### Ключевой вывод

**prependSystemContext НЕ виден в чате!**

Это значит что SESSION SKELETON и SESSION FOCUS в prependSystemContext не должны быть видны в UI.

**Проблема:** Олег видел SESSION SKELETON в чате раньше — значит он inject'ился через другой механизм (возможно prependContext в старой версии кода).

### Решение

Оставить SESSION SKELETON и SESSION FOCUS в prependSystemContext — это безопасно, они не отображаются в UI.

### Следующие шаги

1. Убедиться что SESSION SKELETON не виден в чате после перезапуска gateway
2. Если всё ещё виден — найти где именно он inject'ится
3. Документировать поведение prependSystemContext для будущих разработок


---

## DEBUG режим — 2026-04-06 16:35 UTC

**Цель:** возможность отладки prependSystemContext без изменения кода

**Реализация:** переключатель через переменную окружения `TOTAL_RECALL_DEBUG=1`

**Что показывает:**
1. Содержимое prependSystemContext (CORE, MEMORY, SKELETON, FOCUS — есть/нет)
2. Содержимое SESSION SKELETON (первые 500 символов)

**Включение:**
```bash
export TOTAL_RECALL_DEBUG=1
openclaw gateway restart
```

**Отключение:**
```bash
unset TOTAL_RECALL_DEBUG
openclaw gateway restart
```

**Лог:** `/tmp/total-recall.log`

**План удаления:** через 2-3 дня (2026-04-09) после подтверждения что работает

**Альтернатива:** можно сделать permanent debug logging через конфиг, если нужно


---

## DEBUG режим включён — 2026-04-06 16:46 UTC

**Статус:** ✅ Включён через `TOTAL_RECALL_DEBUG=1`

**Подтверждение:** лог показывает:
```
DEBUG: prependSystemContext contains: CORE=true, MEMORY=true, SKELETON=true, FOCUS=true
```

**План:**
- Оставить включённым на 2-3 дня для мониторинга
- Удалить через `unset TOTAL_RECALL_DEBUG` + `openclaw gateway restart` после 2026-04-09

**Примечание:** DEBUG логирование не влияет на работу системы, только добавляет информацию в `/tmp/total-recall.log`


---

## Ошибка memory-reflect — 2026-04-06 17:17 UTC

**Проблема:** `FileNotFoundError: [Errno 2] No such file or directory: 'python'`

**Причина:** в session_store.py используется `python` вместо `python3`

**Решение:** заменить `python` на `python3` в subprocess.Popen (строка 377)

```python
# Было:
subprocess.Popen(["python", __file__, ...])

# Стало:
subprocess.Popen(["python3", __file__, ...])
```

**Структура memory-reflect:**
```
~/projects/total-recall/skills/memory-reflect/
├── .gitignore
├── config.py
├── kb_store.py
├── memory-daemon.py
├── memory-daemon.service
├── memory-reflect.py
├── migrate_summary.py
├── poetry.lock
├── pyproject.toml
├── session_store.py  ← исправлен
└── store.py
```

**Рабочая версия:** `~/.openclaw/skills/memory-reflect/` (синхронизирована с репозиторием)

**Статус:** ✅ исправлено в системе и репозитории


---

## Очистка репозитория — 2026-04-06 22:58 UTC

**Цель:** удалить устаревшие файлы и папки из корневого каталога

**Удалено:**

**Папки:**
- `prompts/` — устаревшие промпты
- `reports/` — отчёты по тестам
- `tests/` — тестовые файлы
- `config/` — устаревшие конфиги (теперь в skills/memory-reflect/)
- `cypher/` — Cypher queries (устарели, теперь в Python)
- `reranker-api/` — пустая папка
- `scripts/` — устаревшие скрипты

**Файлы:**
- `REPORT.md` — устаревший отчёт
- `TEST-REPORT.md` — устаревший отчёт
- `handoff-2026-03-31-kb.md` — устаревший handoff
- `lossless-claw-analysis-v2.md` — устаревший анализ
- `lossless-claw-analysis.md` — устаревший анализ
- `phi4-mini-load.md` — устаревший тест
- `handler.js.backup.20260405_002856` — backup файл

**Оставлено:**
- `docs/` — актуальная документация
- `skills/` — memory-reflect
- `README.md`, `handler.js`, `index.js`, `openclaw.plugin.json`, `package.json`, `install.sh`, `decisions.md`

**Итого:** удалено 22 файла, 4339 строк

**Статус:** ✅ завершено
