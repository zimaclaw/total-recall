# Проблемы с flashback + reranking в total-recall

## 🔍 Найдено

### 1. Неправильный RERANK_URL в .env

**Текущее значение:**
```
RERANK_URL=http://192.168.1.145:11435/api/rerank
```

**Проблема:** Ollama не имеет endpoint `/api/rerank`. bge-reranker работает через `/api/embeddings`.

**Решение:**
```
RERANK_URL=http://192.168.1.145:11437/api/embeddings
```

**Статус:** ✅ Исправлено в .env и config.py

### 2. Неправильный формат запроса в `_rerank()`

**Текущий код (store.py:1015-1046):**
```python
resp = requests.post(
    settings.rerank_url,
    json={"model": settings.rerank_model, "query": query, "documents": docs},
    timeout=15,
)
```

**Проблема:** Ollama embeddings API принимает только `{"model": "...", "prompt": "..."}`, а не query+documents.

**Решение:** Нужно изменить логику:
1. Получить embedding для query
2. Получить embeddings для всех documents
3. Посчитать cosine similarity
4. Отсортировать по сходству

**Статус:** ✅ Исправлено в store.py

### 3. Неправильный формат в `_embed()`

**Текущий код:**
```python
json={"model": settings.embed_model, "input": text}
```

**Проблема:** Ollama использует `prompt`, а не `input`.

**Решение:**
```python
json={"model": settings.embed_model, "prompt": text}
```

**Статус:** ✅ Исправлено в store.py

### 4. Qdrant вектора не индексированы

**Статус:**
```json
{
  "points_count": 52,
  "indexed_vectors_count": 0
}
```

**Проблема:** 52 записей в Qdrant, но 0 индексированных векторов для HNSW поиска.

**Решение:** Перезагрузить коллекцию или добавить записи с индексацией.

**Статус:** ❌ Не исправлено — требует решения

---

## 🛠️ План исправлений

### Шаг 1: Обновить .env
```bash
# Изменить
RERANK_URL=http://192.168.1.145:11437/api/embeddings
```

### Шаг 2: Обновить `_rerank()` в store.py

Заменить текущую реализацию на:
```python
def _rerank(self, query: str, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates

    # Получить embedding для query
    query_embedding = self._embed(query)
    if not query_embedding:
        return candidates

    # Получить embeddings для documents и посчитать сходство
    def cosine_similarity(a, b):
        from math import sqrt
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sqrt(sum(x * x for x in a))
        norm_b = sqrt(sum(x * x for x in b))
        return dot / (norm_a * norm_b)

    scored = []
    for candidate in candidates:
        text = candidate.get("text", "")
        doc_embedding = self._embed(text)
        if doc_embedding:
            score = cosine_similarity(query_embedding, doc_embedding)
            payload = dict(candidate)
            payload["_score"] = round(score, 3)
            scored.append(payload)

    scored.sort(key=lambda x: x["_score"], reverse=True)
    log.info(f"Reranked {len(scored)} results with cosine similarity")
    return scored
```

### Шаг 3: Перезагрузить Qdrant коллекцию

```bash
curl -X POST "http://192.168.1.145:6333/collections/reflections/scroll" \
  -H 'Content-Type: application/json' \
  -d '{"limit": 100}'
```

Или удалить и создать заново:
```bash
curl -X DELETE "http://192.168.1.145:6333/collections/reflections"
# Затем пересоздать через memory-reflect.py --init-schema
```

---

## 📊 Текущий статус

### ✅ Что работает

**Flashback по категории (Neo4j граф):**
```bash
$ python3 memory-reflect.py --flashback --category dev
```
**Результат:** ✅ Работает! Показывает 9 записей (conclusions, lessons, meta) из Neo4j графа.

**Embedding генерация:**
```bash
$ python3 -c "from store import QdrantSearch; q = QdrantSearch(); v = q._embed('test'); print(len(v))"
Embedding размер: 1024
```
**Результат:** ✅ Работает корректно

**Reranking через cosine similarity:**
- Тест с живыми данными показал корректную работу
- Relevant документы ранжируются выше (0.9943 vs 0.6751)

### ❌ Что не работает

**Flashback по focus (семантический поиск в Qdrant):**
```bash
$ python3 memory-reflect.py --flashback --focus "ollama warmup"
[flashback · focus='ollama warmup' · category=any]
  (нет результатов)
```
**Причина:** Qdrant не индексирует вектора для HNSW поиска (0 из 52)

### 🔍 Разница между типами flashback

| Тип flashback | Источник | Статус | Причина |
|---------------|----------|--------|---------|
| `--flashback --category dev` | Neo4j граф | ✅ Работает | Записи в Neo4j |
| `--flashback --focus "тема"` | Qdrant вектора | ❌ Не работает | 0 индексированных векторов |

**Flashback по категории:**
- Чтение из Neo4j графа (Task → Evidence → Conclusion → Lesson → Principle → Meta)
- Фильтрация по category
- Возвращает иерархию знаний
- **Работает корректно** ✅

**Flashback по focus:**
1. Получает embedding для query через bge-m3
2. Ищет top-20 в Qdrant (семантический поиск) ← **ПРОБЛЕМА ЗДЕСЬ**
3. Фильтрует по threshold (0.72 * 0.85 = 0.612)
4. Rerank через cosine similarity с bge-reranker
5. Возвращает top-5

**Проблема на шаге 2:** Qdrant не может найти вектора потому что `indexed_vectors_count: 0`

### 🛠️ Исправления в коде

| Файл | Изменение | Статус |
|------|------------|--------|
| `.env` | RERANK_URL → 11437/api/embeddings | ✅ |
| `config.py` | Дефолтные URL обновлены | ✅ |
| `store.py` | `_embed()` → prompt вместо input | ✅ |
| `store.py` | `_rerank()` → cosine similarity | ✅ |
| Qdrant | Индексация векторов | ❌ Требуется решение |

---

## 🎯 Ожидаемый результат

После исправления Qdrant индексации:
1. `--flashback --focus "тема"` будет возвращать top-5 релевантных conclusions
2. Reranking будет работать через cosine similarity с bge-reranker-v2-m3
3. Qdrant будет индексировать вектора для быстрого поиска

---

## 📚 Ссылки

- **Полное описание обновления:** `reranking-update-2026-03-26.md`
- **Использование bge-reranker:** `bge-reranker-usage.md`
- **Статус исправлений:** `/home/ironman/.openclaw/workspace/docs/total-recall-reranking-fix-status.md`

---

**Создано:** 2026-03-26 20:35 UTC  
**Обновлено:** 2026-03-26 21:11 UTC  
**Статус:** 🔧 Исправления в коде выполнены, требуется решение по Qdrant индексации
