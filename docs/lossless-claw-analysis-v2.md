# Lossless-Claw Analysis — Updated with Critique

**Дата:** 2026-03-27 00:11 UTC (обновлено)  
**Версия:** 2.0 (критика и улучшения)

---

## 📋 Executive Summary (Updated)

**Рекомендация:** 🟡 **Условно рекомендовано** — с конкретным планом тестирования

**Обновлённые выводы:**
1. ✅ Полностью заменяет context-compactor (подтверждено в docs)
2. ⚠️ Совместимость с total-recall **нужно проверить в коде**
3. ✅ Ресурсные требования минимальны (8-9 LLM calls/день, ~$0 local)
4. ✅ Performance impact可控 (15-25 сек/compaction, можно отключить auto)

---

## 🔍 Критика предыдущего анализа

### Что было слабо

1. **Поверхностное изучение:** Только README/docs, не реальный код
2. **Пессимизм без проверки:** Предположила конфликт с total-recall без анализа
3. **Нет ресурсных расчётов:** Не посчитала LLM calls и задержки
4. **Размытый backup:** "Настроить backup" без конкретики
5. **Нет migration path:** Не описала как мигрировать
6. **Нет performance impact:** Не оценила задержки
7. **Нет решения для конфликта:** Если будет — что делать?
8. **Размытое тестирование:** "Тестировать на test session" — неконкретно
9. **Нет cost analysis:** Не оценила стоимость

### Что добавлено

✅ Детальный расчёт ресурсных требований  
✅ Конкретная backup strategy  
✅ Пошаговый migration path  
✅ Performance impact analysis  
✅ Конкретные решения для total-recall конфликта  
✅ 7-дневный тестовый план  
✅ Cost analysis (local vs cloud)  
✅ Updated recommendation с условиями

---

## 1. Ресурсные требования (NEW)

### LLM calls расчёт

**Сценарий:** 50 сообщений/день в активной сессии

| Тип | Частота | Токенов | Время | Всего/день |
|-----|---------|---------|-------|------------|
| **Leaf summary** | Каждые 8 сообщений | ~1200 | 5-10 сек | 6-7 calls |
| **Condensed summary** | Каждые 4 leaf | ~2000 | 10-15 сек | 1-2 calls |
| **Total** | - | - | - | **8-9 calls/день** |

**GPU usage:**
- qwen3:14b уже загружен в GPU (12.61GB VRAM)
- Summarization использует тот же контекст
- **Нет доп. VRAM requirements**

**Time impact:**
- 8-9 calls × ~10 секунд = **~90-120 секунд/день** суммаризации
- Распределено на весь день (не заметно в real-time)

---

### Cost analysis (NEW)

**Local inference (ollama):**
- **Cost:** $0 (local GPU)
- **Time:** ~100-150 секунд/день
- **GPU:** qwen3:14b уже загружен

**Cloud API (если нужен cloud):**
```
12K токенов/день × $0.0000025/token (Anthropic Haiku) = $0.03/день
= ~$1/месяц
```

**Вывод:** Resource impact минимальный для local inference.

---

## 2. Performance impact (NEW)

### Задержки от суммаризации

**Per compaction:**
- Leaf summary: 5-10 секунд
- Condensed summary: 10-15 секунд
- **Total:** 15-25 секунд

**User experience:**
- User видит delay после каждого 8-10 сообщений
- Может быть заметно в real-time conversation

**Решение:** Отключить auto compaction, запускать вручную:
```bash
export LCM_AUTOCOMPACT_DISABLED=true
# Запускать вручную ночью:
0 3 * * * openclaw /compact
```

---

## 3. Backup strategy (UPDATED)

### Конкретный план

```bash
# 1. Ежедневный backup (cron)
echo "0 2 * * * cp ~/.openclaw/lcm.db ~/.openclaw/lcm.db.backup.$(date +%Y%m%d)" >> ~/.cron.d/lcm-backup

# 2. Ротация (30 дней)
echo "0 3 * * 0 find ~/.openclaw/lcm.db.backup.* -mtime +30 -delete" >> ~/.cron.d/lcm-backup

# 3. Перед major changes
cp ~/.openclaw/lcm.db ~/.openclaw/lcm.db.pre-upgrade

# 4. Restore procedure
sqlite3 ~/.openclaw/lcm.db.backup.YYYYMMDD ".dump" | sqlite3 ~/.openclaw/lcm.db
```

**Retention:** 30 дней daily backups + pre-upgrade backup

---

## 4. Migration path (NEW)

### Пошаговый план миграции

**Фаза 0: Подготовка (1 час)**
```bash
# 1. Backup текущей сессии
mkdir ~/backup/openclaw-$(date +%Y%m%d)
cp -r ~/.openclaw/sessions/agent:main:direct/*.jsonl ~/backup/openclaw-*/
cp ~/.openclaw/lcm.db ~/backup/openclaw-*/ 2>/dev/null || echo "No existing LCM"

# 2. Проверить текущий контекст
wc -l ~/.openclaw/sessions/agent:main:direct/*.jsonl
```

**Фаза 1: Установка (15 минут)**
```bash
# 1. Установить plugin
openclaw plugins install @martian-engineering/lossless-claw

# 2. Настроить config
cat >> ~/.openclaw/openclaw.json << 'EOF'
{
  "plugins": {
    "entries": {
      "lossless-claw": {
        "enabled": true,
        "config": {
          "freshTailCount": 32,
          "contextThreshold": 0.75,
          "incrementalMaxDepth": -1,
          "summaryModel": "qwen3:14b",
          "summaryProvider": "ollama"
        }
      }
    },
    "slots": {
      "contextEngine": "lossless-claw"
    }
  }
}
EOF

# 3. Перезапустить gateway
openclaw gateway restart
```

**Фаза 2: Bootstrap reconciliation (автоматически)**
```bash
# 1. Отправить тестовое сообщение
# LCM автоматически импортирует JSONL в SQLite

# 2. Проверить импорт
sqlite3 ~/.openclaw/lcm.db "SELECT COUNT(*) FROM conversations;"
sqlite3 ~/.openclaw/lcm.db "SELECT COUNT(*) FROM messages WHERE conversation_id = 1;"
```

**Фаза 3: Валидация (1 день)**
```bash
# 1. Проверить compaction
sqlite3 ~/.openclaw/lcm.db "SELECT depth, COUNT(*) FROM summaries GROUP BY depth;"

# 2. Проверить context assembly
# Отправить сообщение и проверить что summaries в контексте

# 3. Проверить tools
lcm_grep(pattern: "test")
lcm_describe(id: "sum_...")
```

**Фаза 4: Rollback (если проблемы)**
```bash
# 1. Вернуть contextEngine
{
  "plugins": {
    "slots": {
      "contextEngine": "legacy"
    }
  }
}

# 2. Перезапустить
openclaw gateway restart

# 3. JSONL session file intact → no data loss
```

---

## 5. Total-recall конфликт — решения (UPDATED)

### Опция 1: Модифицировать total-recall

**Проблема:** `prependContext` может быть потерян при assembly

**Решение:** Использовать `lcm_grep` для recall вместо direct injection:

```javascript
// Вместо:
return { prependContext: flashbackResult };

// Использовать:
// 1. Store flashback в LCM как special message
// 2. Или использовать lcm_grep для recall при необходимости
```

**Плюсы:** Нативная интеграция с LCM  
**Минусы:** Требует модификации total-recall

---

### Опция 2: Модифицировать lossless-claw

**Решение:** Добавить hook для prependContext integration

```typescript
// В engine.ts assembleContext():
const prependContext = await api.hooks.emit('before_context_assembly', { event });
if (prependContext?.content) {
  messages.unshift({ role: 'user', content: prependContext.content });
}
```

**Плюсы:** Чистое решение, работает для всех plugins  
**Минусы:** Требует fork lossless-claw

---

### Опция 3: Hybrid approach

**Решение:** Total-recall для active session, lossless-claw для archive

```javascript
// Total-recall: flashback для current session context
// Lossless-claw: long-term history preservation

// Синхронизация:
// 1. Total-recall пишет conclusions в Neo4j/Qdrant
// 2. Lossless-claw сохраняет raw messages в SQLite
// 3. Нет конфликта — разные слои
```

**Плюсы:** Нет конфликта, best of both worlds  
**Минусы:** Сложная архитектура, дублирование

---

### Рекомендуемый подход

**Step 1:** Проверить как lossless-claw обрабатывает hooks
```bash
# Прочитать index.ts и engine.ts
# Найти emit/on для before_agent_start
```

**Step 2:** Если prependContext поддерживается → использовать как есть

**Step 3:** Если не поддерживается → Option 3 (hybrid)

**Step 4:** Если hybrid слишком сложный → Option 1 (модифицировать total-recall)

---

## 6. Тестовый план (UPDATED)

### 7-дневный тестовый план

**День 1-2: Установка и базовая проверка**
- [ ] Установить plugin
- [ ] Проверить bootstrap reconciliation
- [ ] Проверить compaction работает
- [ ] Проверить lcm_grep, lcm_describe
- [ ] Замерить задержки от суммаризации

**День 3-4: Total-recall совместимость**
- [ ] Протестировать flashback с lossless-claw
- [ ] Проверить prependContext сохраняется
- [ ] Проверить Neo4j/Qdrant интеграция
- [ ] Если конфликт → выбрать решение (Option 1/2/3)

**День 5-6: Performance и стабильность**
- [ ] Замерить LLM calls/день
- [ ] Проверить GPU usage
- [ ] Проверить backup/restore
- [ ] Проверить recovery после crash

**День 7: Решение**
- [ ] Если всё ок → rollout на production
- [ ] Если проблемы → rollback или модификация

---

## 7. Updated recommendation

### 🟡 **Условно рекомендовано** — с конкретными условиями

**Условия для установки:**

1. ✅ **Проверить код lossless-claw:**
   - Прочитать `index.ts` и `engine.ts`
   - Найти как обрабатываются hooks от других plugins
   - Проверить поддержку `prependContext`

2. ✅ **Создать test session:**
   - Не использовать main session для тестов
   - Иметь rollback plan

3. ✅ **Настроить monitoring:**
   - Track LLM calls/день
   - Track compaction time
   - Track context size

4. ✅ **Следовать 7-дневному тестовому плану**

---

### 📊 Updated评分

| Критерий | Оценка | Комментарий |
|----------|--------|-------------|
| **Решает проблему** | ✅ 10/10 | Идеальное решение |
| **Ресурсные требования** | ✅ 9/10 | Минимальные (local) |
| **Performance impact** | ⚠️ 7/10 | 15-25 сек/compaction,可控 |
| **Совместимость со стеком** | ⚠️ 6/10 | Требуется проверка |
| **Риск для total-recall** | ⚠️ 5/10 | Зависит от реализации |
| **Сложность внедрения** | ⚠️ 6/10 | Требует staged rollout |
| **Общее** | 🟡 7.2/10 | Условно рекомендовано |

---

## 🎯 Updated финальная рекомендация

**Действовать по плану:**

1. **Неделя 1:** Изучить код lossless-claw (hooks, assembly)
2. **Неделя 2:** Test session + 7-дневный тест
3. **Неделя 3:** Решение (production или rollback)

**Если total-recall конфликт:**
- Option 1: Модифицировать total-recall (использовать lcm_grep)
- Option 2: Hybrid approach (total-recall + lossless-claw)
- Option 3: Отказаться от lossless-claw

**Альтернатива:** Увеличить `context.maxTokens` до 150K и жить с periodic compaction.

---

**Обновлено:** 2026-03-27 00:11 UTC  
**Статус:** Анализ улучшен с критикой и конкретными планами
