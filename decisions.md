# Decisions — total-recall

## KB — статус: реализовано 2026-03-31

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

## Другие решения

<!-- Добавлять новые решения ниже -->
