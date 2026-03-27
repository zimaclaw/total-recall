# Lossless-Claw Analysis for OpenClaw Stack

**Дата:** 2026-03-27 00:05 UTC  
**Цель:** Оценить совместимость lossless-claw с текущим стеком OpenClaw

---

## 📋 Executive Summary

**Рекомендация:** ⚠️ **С осторожностью** — lossless-claw решает проблему раздувания контекста, но есть риски для memory stack.

**Ключевые выводы:**
1. ✅ Полностью заменяет context-compactor (не конфликтует)
2. ⚠️ Совместимость с llamacpp требует тестирования
3. ⚠️ Риск конфликта с total-recall plugin
4. ✅ Отличная архитектура для long-running sessions

---

## 1. Как lossless-claw работает с llamacpp

### Архитектура суммаризации

Lossless-claw использует **DAG-based summarization**:
- **Leaf summaries** (depth 0): 8-12 сообщений → ~1200 токенов
- **Condensed summaries** (depth 1+): 4+ summaries → ~2000 токенов
- **Escalation**: normal → aggressive → deterministic truncation

### Совместимость с llamacpp :8180

**Проблема:** Lossless-claw ожидает Anthropic/OpenAI формат ответов.

**Решение:** Использовать **ollama :11434 (qwen3:14b)** для суммаризации:

```bash
export LCM_SUMMARY_MODEL=qwen3:14b
export LCM_SUMMARY_PROVIDER=ollama
export LCM_SUMMARY_BASE_URL=http://192.168.1.145:11434
```

**Почему ollama, а не llamacpp:**
1. **API совместимость:** Ollama API ближе к OpenAI, проще интеграция
2. **Ресурсы:** qwen3:14b уже загружен в GPU, не требует дополнительной VRAM
3. **Скорость:** 14B модель быстрее 27B для суммаризации
4. **Качество:** Qwen3:14b достаточно хорош для суммаризации (не требует reasoning)

**Альтернатива:** Если нужно использовать llamacpp :8180:
- Требуется кастомный provider в OpenClaw
- Нужно проверить нормализацию ответов ( Anthropic/OpenAI text blocks)
- Риск: плохие summary → cascade failures при condensation

---

## 2. Совместимость с context-compactor

### Архитектурное различие

| Feature | context-compactor | lossless-claw |
|---------|-------------------|---------------|
| **Подход** | Sliding window truncation | DAG-based summarization |
| **Сохраняет историю** | ❌ Да (выбрасывает старые) | ✅ Полностью (SQLite + summaries) |
| **Контекст** | 100K max tokens → summary | Unlimited (summaries + fresh tail) |
| **Восстановление** | ❌ Нет | ✅ lcm_expand_query tool |
| **Хранение** | JSONL session file | SQLite database (lcm.db) |

### Конфликт?

**Нет конфликта — полная замена:**

```json
{
  "plugins": {
    "slots": {
      "contextEngine": "lossless-claw"  // заменяет "legacy" (context-compactor)
    }
  }
}
```

**Процесс миграции:**
1. Установить lossless-claw plugin
2. Сменить `contextEngine` slot на `lossless-claw`
3. Перезапустить OpenClaw
4. **Bootstrap reconciliation:** LCM импортирует существующие JSONL сессии в SQLite
5. Старый context-compactor больше не используется

**Важно:** Это **mutually exclusive** — нельзя использовать оба одновременно.

---

## 3. Риски для memory stack (Neo4j + Qdrant + total-recall)

### ⚠️ КРИТИЧЕСКИЙ РИСК: Конфликт контекстных hooks

**Проблема:** Total-recall и lossless-claw оба работают с контекстом, но по-разному:

| Plugin | Hook | Действие |
|--------|------|----------|
| **total-recall** | `before_agent_start` | Добавляет flashback в `prependContext` |
| **lossless-claw** | `contextEngine` | Ассемблирует контекст из summaries + messages |

**Риск:** Lossless-claw **перехватывает** сборку контекста. Total-recall's `prependContext` может быть:
1. ✅ **Поддержан** — если lossless-claw интегрирует prependContext в assembled context
2. ❌ **Игнорирован** — если lossless-claw полностью контролирует контекст

**Проверка:** Нужно изучить `index.ts` lossless-claw, как он обрабатывает `prependContext` от других plugins.

### ⚠️ Риск 2: Изменение структуры сообщений

**Total-recall ожидает:**
```json
{
  "prompt": "<текст пользователя>",
  "messages": [...],
  "context": [...]
}
```

**Lossless-claw меняет:**
```json
{
  "messages": [
    {"role": "user", "content": "<summary id=...>...</summary>"},
    {"role": "user", "content": "<raw message>"},
    ...
  ]
}
```

**Влияние:**
- ✅ Total-recall's `inferCategory(prompt)` работает — prompt всё ещё доступен
- ⚠️ Total-recall's flashback injection может быть **потерян** при assembly

### ⚠️ Риск 3: Session reconciliation

**Total-recall использует:**
- `sessionFile` для отслеживания истории
- `messages` для анализа контекста

**Lossless-claw:**
- Хранит всё в SQLite (`lcm.db`)
- JSONL session file существует, но **не является source of truth**

**Риск:** Если total-recall пытается читать `sessionFile` напрямую — данные могут быть устаревшими.

---

## 4. Рекомендуемая конфигурация для контекста 163K

### Базовая конфигурация

```bash
# Fresh tail — последние сообщения которые никогда не compact
export LCM_FRESH_TAIL_COUNT=32  # ~6-8K токенов (защищённый контекст)

# Context threshold — когда начинать compaction
export LCM_CONTEXT_THRESHOLD=0.75  # 122K токенов (75% от 163K)

# Incremental max depth — глубина автоматической condensation
export LCM_INCREMENTAL_MAX_DEPTH=-1  # unlimited (каскадная condensation)

# Leaf chunk — сколько токенов за раз summarizing
export LCM_LEAF_CHUNK_TOKENS=20000  # ~10-12 сообщений

# Summary targets
export LCM_LEAF_TARGET_TOKENS=1200  # leaf summaries
export LCM_CONDENSED_TARGET_TOKENS=2000  # condensed summaries

# Model configuration
export LCM_SUMMARY_MODEL=qwen3:14b
export LCM_SUMMARY_PROVIDER=ollama
export LCM_SUMMARY_BASE_URL=http://192.168.1.145:11434
```

### Объяснение значений

| Параметр | Значение | Обоснование |
|----------|----------|-------------|
| `freshTailCount=32` | 32 сообщения | Достаточно для continuity (tool calls, multi-step tasks) |
| `contextThreshold=0.75` | 122K токенов | Leaves 41K headroom для model response |
| `incrementalMaxDepth=-1` | unlimited | Автоматическая каскадная condensation — не требует ручного `/compact` |
| `leafChunkTokens=20000` | 20K токенов | Оптимальный баланс detail vs summary quality |
| `leafTargetTokens=1200` | 1.2K токенов | Достаточно detail для leaf summaries |
| `condensedTargetTokens=2000` | 2K токенов | Higher-level summaries могут быть больше |

### Session reset configuration

```json
{
  "session": {
    "reset": {
      "mode": "idle",
      "idleMinutes": 10080  // 7 дней — long-running sessions
    }
  }
}
```

---

## 5. Оценка: Стоит ли ставить?

### ✅ Преимущества

1. **Lossless history** — ничего не теряется, всё в SQLite
2. **Scalable context** — 163K+ токенов без проблем
3. **Expansion tools** — `lcm_grep`, `lcm_describe`, `lcm_expand_query` для recall
4. **Automatic compaction** — не требует ручного вмешательства
5. **Large file handling** — intercepts files >25K tokens, stores separately

### ⚠️ Риски

1. **Конфликт с total-recall** — нужно проверить совместимость hooks
2. **Новая dependency** — SQLite database (`lcm.db`) — нужно backup
3. **LLM calls overhead** — суммаризация требует дополнительных вызовов
4. **Сложность отладки** — DAG-based система сложнее sliding window

### ❌ Подводные камни

1. **Total-recall может сломаться** — если prependContext не поддерживается
2. **Требуется тестирование** — совместимость с llamacpp/ollama стек
3. **Migration cost** — нужно проверить bootstrap reconciliation
4. **No rollbacks** — если summary плохой, cascade failures

---

## 6. Рекомендация

### 🟡 **Условно рекомендовано** — с условиями

**Условия для установки:**

1. ✅ **Проверить total-recall совместимость:**
   - Изучить как lossless-claw обрабатывает `prependContext`
   - Протестировать flashback injection с lossless-claw
   - Если конфликт — нужно модифицировать total-recall или lossless-claw

2. ✅ **Протестировать с ollama :11434:**
   - Настроить `LCM_SUMMARY_MODEL=qwen3:14b`
   - Проверить качество summaries
   - Проверить escalation (normal → aggressive → truncation)

3. ✅ **Backup strategy:**
   - Настроить backup для `lcm.db`
   - Тестировать восстановление из backup

4. ✅ **Staged rollout:**
   - Сначала на test session
   - Проверить 1-2 недели работы
   - Затем на production

### 📋 План внедрения

**Фаза 1: Подготовка (1 день)**
- [ ] Изучить `index.ts` lossless-claw — как обрабатывается `prependContext`
- [ ] Создать test session для экспериментов
- [ ] Настроить backup для `lcm.db`

**Фаза 2: Тестирование (1 неделя)**
- [ ] Установить lossless-claw на test session
- [ ] Настроить `LCM_SUMMARY_MODEL=qwen3:14b`
- [ ] Протестировать compaction на реальной сессии
- [ ] Проверить `lcm_grep`, `lcm_describe`, `lcm_expand_query`
- [ ] **Критично:** Проверить total-recall flashback с lossless-claw

**Фаза 3: Решение (после теста)**
- Если total-recall работает → rollout на production
- Если конфликт → модифицировать total-recall или отказаться от lossless-claw

---

## 7. Альтернативы

Если lossless-claw несовместим с total-recall:

### Option A: Улучшить context-compactor

```json
{
  "context": {
    "maxTokens": 100000,
    "summaryMaxTokens": 2000,
    "summaryModel": "qwen2.5:7b"
  }
}
```

**Плюсы:**
- Простая интеграция
- Нет конфликта с total-recall

**Минусы:**
- Теряется история
- Нет expansion tools

### Option B: Hybrid approach

Использовать **оба** подхода:
- **Lossless-claw** для long-term history (archive)
- **Total-recall** для active session context
- **Manual sync** между ними

**Плюсы:**
- Лучшее из обоих миров
- Нет конфликта

**Минусы:**
- Сложная архитектура
- Требует manual sync

---

## 📊 Итоговая оценка

| Критерий | Оценка | Комментарий |
|----------|--------|-------------|
| **Решает проблему** | ✅ 10/10 | Идеальное решение для раздувания контекста |
| **Совместимость со стеком** | ⚠️ 6/10 | Требуется тестирование с ollama |
| **Риск для total-recall** | ⚠️ 4/10 | Высокий риск конфликта hooks |
| **Сложность внедрения** | ⚠️ 6/10 | Требует staged rollout |
| **Общее** | 🟡 6.5/10 | Условно рекомендовано |

---

## 🎯 Финальная рекомендация

**Не устанавливать lossless-claw прямо сейчас.**

**Причины:**
1. **Риск сломать total-recall** — flashback критичен для работы Пятницы
2. **Нет срочности** — текущий context-compactor работает (просто теряет историю)
3. **Требуется исследование** — нужно проверить совместимость hooks

**Действия:**
1. **Создать issue** в lossless-claw repo — спросить про `prependContext` support
2. **Почитать код** — изучить как lossless-claw обрабатывает hooks от других plugins
3. **Если compatible** → тестировать на test session
4. **Если incompatible** → модифицировать total-recall или отказаться

**Альтернатива:** Увеличить `context.maxTokens` до 150K и жить с periodic compaction.

---

**Записано:** 2026-03-27 00:05 UTC  
**Статус:** Анализ завершён, требуется проверка совместимости перед установкой
