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

### 3. Qdrant вектора не индексированы

**Статус:**
```json
{
  "points_count": 52,
  "indexed_vectors_count": 0
}
```

**Проблема:** 52 записей в Qdrant, но 0 индексированных векторов для HNSW поиска.

**Решение:** Перезагрузить коллекцию или добавить записи с индексацией.

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

- ✅ bge-m3 работает на 11435 (embeddings)
- ✅ bge-reranker-v2-m3 работает на 11437 (embeddings)
- ✅ 52 записей в Qdrant
- ❌ 0 индексированных векторов
- ❌ RERANK_URL указывает на несуществующий endpoint
- ❌ `_rerank()` использует неправильный API формат

---

## 🎯 Ожидаемый результат

После исправлений:
1. `--flashback --focus "тема"` будет возвращать top-5 релевантных conclusions
2. Reranking будет работать через cosine similarity с bge-reranker-v2-m3
3. Qdrant будет индексировать вектора для быстрого поиска

---

**Создано:** 2026-03-26 20:35 UTC  
**Статус:** 🔍 Анализ завершён, готово к исправлению
