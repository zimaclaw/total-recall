# Qdrant индексация: решение проблемы

**Дата:** 2026-03-26  
**Статус:** ✅ Выполнено  
**Время решения:** ~20 минут

---

## Проблема

```
points_count: 54
indexed_vectors_count: 0
```

**Flashback по focus не работал** — возвращал 0 результатов.

---

## Причины

### 1. Неправильная конфигурация HNSW
- `indexing_threshold: 10000` — Qdrant не индексирует пока записей < 10000
- `full_scan_threshold: 10000` — используется full scan вместо HNSW

### 2. Неправильный threshold для similarity
- `SIMILARITY_THRESHOLD=0.72` → `0.72 * 0.85 = 0.612`
- Реальные scores: макс. `0.5280` — все ниже threshold
- **Причина:** 0.72 слишком высокий для cosine similarity с embeddings

### 3. Неправильное свойство в Neo4j
- Использовалось `text` вместо `insight` у Conclusion
- Все 54 добавления падали с ошибкой

---

## Решение

### Шаг 1: Пересоздание коллекции
```bash
# Удалить старую
curl -X DELETE "http://192.168.1.145:6333/collections/reflections"

# Создать новую с правильными настройками
indexing_threshold: 10  # ← Было 10000
full_scan_threshold: 10  # ← Было 10000
vacuum_min_vector_number: 100  # ← Минимум
```

### Шаг 2: Переиндексация
```python
# Использовать insight вместо text
query = """
MATCH (c:Conclusion)
RETURN c.insight AS text, c.category AS category, ...
"""
```

**Результат:** 54 conclusions добавлено, 0 ошибок

### Шаг 3: Исправление threshold
```bash
# .env
SIMILARITY_THRESHOLD=0.40  # ← Было 0.72
```

---

## Результат

### Статус Qdrant
```
points_count: 54
indexed_vectors_count: 54  # ✅
```

### Flashback по focus — работает

**Тест: "ollama warmup"**
```
[flashback · focus='ollama warmup' · category=any]
  [score=0.53] Для управления моделями ollama достаточно API...
  [score=0.51] bge-reranker-v2-m3 в ollama работает как embedding модель...
  [score=0.45] Всегда проверяй реальные размеры моделей через ollama API...
  [score=0.45] Документация должна отражать реальные измерения...
  [score=0.45] Ollama не поддерживает native reranking...
```

**Тест: "порт конфигурация"**
```
[flashback · focus='порт конфигурация' · category=any]
  [score=0.55] перед сменой порта проверять занятость через ss -tlnp
  [score=0.49] перед изменением сетевых правил сохранять текущее состояние...
  [score=0.48] перед reload nginx всегда проверять конфиг через nginx -t
```

---

## Изменения

| Файл/ресурс | Изменение |
|-------------|------------|
| Qdrant collection reflections | Пересоздана с `indexing_threshold: 10` |
| `.env` | `SIMILARITY_THRESHOLD=0.40` (было 0.72) |
| Qdrant data | 54 conclusions переиндексированы |

---

## Выводы

1. **HNSW индексация** требует `indexing_threshold <= points_count`
2. **Cosine similarity** с embeddings работает в диапазоне 0.3-0.6, не 0.7-0.9
3. **Проверять свойства Neo4j** перед использованием — `insight` ≠ `text`
4. **Flashback по category** (Neo4j) и **по focus** (Qdrant) — разные механизмы

---

**Обновлено:** 2026-03-26 22:05 UTC
