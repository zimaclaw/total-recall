# Архитектурные решения — OpenClaw Personal Assistant

*Зафиксированные решения и отказы. Обновлено: 2026-03-30*

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
- VECTOR INDEX ON Conclusion(embedding) — нативные векторы
- Контекстный embedding: "запрос → outcome"
- Один Cypher запрос: vector search + граф обход вверх

### PostgreSQL + pgvector · .145:5432
Сессии + KB. Долгосрочное хранилище.
```
sessions          — одна строка на сессию
session_messages  — чистые промпты (без injected контекста)
session_vectors   — pgvector embeddings для фокуса
kb_hot            — найденное агентом, 7 дней (не реализовано)
kb_cold           — промотированное, вечное (не реализовано)
```

### Qdrant · .145:6333
Только KB векторы (когда KB будет реализовано).
- Коллекция `reflections` — **удалена** (заменено Neo4j vectors)
- Коллекция `memories` (mem0) — **закрыта** (не мигрировать)

### CORE.md · файл на диске
~2K. Всегда в system prompt. Редактируется агентом через tool call.

---

## Что удалено / закрыто

### mem0 — отказ
**Причина:** recall не работал, данные устарели, архитектура заменена.  
**Что сделано:** коллекция `memories` в Qdrant закрыта без миграции.  
**Модель `qwen3:4b-mem0`** — убрана из стека.

### Qdrant reflections — удалена
**Причина:** заменено Neo4j нативными векторами.  
**Что сделано:** коллекция `reflections` удалена.

### qwen3:14b reflect_model — заменяется
**Причина:** Qwen3.5-27B умнее, зоопарк моделей не нужен.  
**Статус:** заменяется слотом 1 на .145 (не реализовано).

### curator hook (файловый) — удалён
**Причина:** конфликтовал с total-recall plugin, дублировал логику.  
**Что сделано:** `rm -rf ~/.openclaw/hooks/curator`

### allowPromptInjection в openclaw.json — не существует
**Причина:** ключ вызывает ошибку валидации конфига.  
**Решение:** `before_prompt_build` работает через plugin API без доп. настроек.

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

## Curator — сборка окна (текущая реализация)

```
[1] CORE.md        ~2K    всегда · начало контекста
[2] Flashback      4-8K   Neo4j: опыт по категории промпта
[3] Скелет         2-4K   PostgreSQL: все промпты пользователя
[4] Фокус          4-8K   pgvector: семантически релевантный кусок

→ prependContext → Пятнице · итого ~12-22K из 165K
```

**Порядок:** важное в начало. CORE.md первым. Текущий промпт последним (OpenClaw добавляет сам).

---

## Граница сессии

**Решение:** явный сигнал от пользователя — команда `/new`.

**Почему не таймаут:** непредсказуемо, пользователь лучше знает когда начинается новый контекст.

**Реализация:** хук `command:new` создаёт новую запись в `sessions`. Все сообщения до `/new` относятся к предыдущей сессии.

---

## sessionId в message_received — решение

**Проблема:** в хуке `message_received` `sessionId` недоступен — он разрешается OpenClaw позже.

**Решение:** читать `~/.openclaw/agents/main/sessions/sessions.json` напрямую.

```javascript
// sessions.json формат:
// { "agent:main:main": { "sessionId": "uuid", ... } }
const store = JSON.parse(readFileSync(storePath));
const entry = Object.values(store).find(e => e?.sessionId && key.includes(channelId));
return entry.sessionId;
```

**Почему не internal hook:** `triggerInternalHook` — внутренняя функция core, недоступна плагинам.

---

## Прошлые сессии — архивирование

**Решение (запланировано):** синхронизировать `.jsonl` файлы в PostgreSQL при команде `/new`.

**Почему:** OpenClaw удаляет старые `.jsonl` файлы. PostgreSQL — долгосрочный архив.

**Почему не читать `.jsonl` напрямую:** user сообщения в `.jsonl` содержат injected контекст (`=== MEMORY CONTEXT ===`). Файлы загрязнены — нельзя использовать для скелета.

---

## Neo4j vector index — статус

**Реализовано:** 2026-03-31  
**Индекс:** `VECTOR INDEX conclusion_embedding ON Conclusion(embedding)` (dimensions: 1024, cosine)  
**Embeddings перегенерированы** для всех 4 уровней в контекстном формате:

| Уровень | Формат |
|---------|--------|
| Conclusion | `"goal: {goal} \| outcome: {outcome} \| insight: {insight}"` |
| Lesson | `"lesson: {principle} \| mastery: {mastery}"` |
| Principle | `"principle: {statement} \| category: {category}"` |
| Meta | `"meta: {statement}"` |

---

## KB — статус

**Запланировано:** kb_hot + kb_cold таблицы в PostgreSQL + Qdrant knowledge_base.  
**Инструменты агента:** kb_save(), kb_promote(), kb_fetch().  
**Статус:** не реализовано.

---

## Flashback — иерархический формат и пороги

### Почему не плоский список

Память агента имеет 4 уровня: Conclusion → Lesson → Principle → Meta. Плоский список смешивает все уровни без структуры — агент не понимает что конкретный опыт, а что абстрактный принцип.

### Как работает сборка

Vector search по запросу пользователя находит релевантные Conclusion. От них граф обходит вверх через `ABSTRACTED_TO`: Conclusion → Lesson → Principle → Meta. Principle и Meta не ищутся напрямую по смыслу запроса — они всплывают как контекст найденного опыта.

Это решает фундаментальную проблему: Principle и Meta сформулированы абстрактно и семантически далеки от любого конкретного запроса. Vector search их просто не найдёт. Но они нужны — они дают агенту широкий взгляд перед действием.

### Дедупликация

Несколько Conclusion могут вести к одному Principle или Meta. При сборке запоминаем ID уже добавленных узлов — каждый Principle и Meta попадает в контекст только один раз.

### Пороги и лимиты

| Уровень | Порог | Лимит |
|---------|-------|-------|
| Conclusion | similarity > 0.65 | топ-5 |
| Lesson | conf > 0.60 | топ-2 |
| Principle | conf > 0.70 | топ-1 (самый высокий conf) |
| Meta | только если макс similarity Conclusion > 0.80 | топ-1 |

Meta показывается только когда найденный опыт действительно релевантен — иначе абстрактный метапринцип будет появляться в каждом flashback и превратится в шум.

### Порядок подачи и логика вывода

Conclusion → Lesson → Principle → Meta

От конкретного к абстрактному — агент сначала видит реальный опыт,
потом принцип выведенный из него.

**Логика вывода Conclusion и Lesson:**

Lesson выведен из одного Conclusion + текст похож:
  → показываем только Lesson (mastery + applied_count богаче чем сырой Conclusion)
  → Conclusion пропускаем — он уже поглощён Lesson

Lesson выведен из нескольких Conclusion:
  → показываем оба
  → Conclusion дают конкретику разных эпизодов, Lesson даёт обобщение

**Почему не меняем промпт рефлексии:**
Lesson из одного Conclusion — это нормально и ценно. Один серьёзный сбой
уже достаточен для вывода принципа. Проблема была не в качестве рефлексии
а в том что flashback показывал и источник и вывод одновременно.

**Пороги:**

| Уровень | Порог | Лимит |
|---------|-------|-------|
| Conclusion | similarity > 0.65 | топ-5 |
| Lesson | conf > 0.60 | топ-2 |
| Principle | conf > 0.70 | топ-1 (самый высокий conf) |
| Meta | только если макс similarity > 0.80 | топ-1 |

### Формат embedding по уровням

| Уровень | Формат embedding |
|---------|------------------|
| Conclusion | `"goal: {goal} \| outcome: {outcome} \| insight: {insight}"` |
| Lesson | `"lesson: {principle} \| mastery: {mastery}"` |
| Principle | `"principle: {statement} \| category: {category}"` |
| Meta | `"meta: {statement}"` |

Conclusion embedится с контекстом задачи — это даёт точное совпадение по намерению пользователя, не по словам. Principle и Meta embedятся для возможного прямого поиска в будущем, но сейчас достигаются только через граф.
