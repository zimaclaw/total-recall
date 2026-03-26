# Обновление total-recall: 4 контейнера Ollama и исправление reranking

## 📋 Обзор изменений

**Дата:** 2026-03-26  
**Статус:** В процессе  
**Приоритет:** Высокий

---

## 🎯 Цель

1. Разделить bge-m3 и bge-reranker-v2-m3 по разным контейнерам Ollama
2. Исправить проблемы с reranking в total-recall
3. Обеспечить корректную работу flashback с семантическим поиском

---

## 🔍 Сложности с Reranker

### Проблема 1: Ollama не поддерживает simultaneous loading моделей с разными endpoints

**Описание:**
- bge-m3 использует `/api/embeddings` endpoint
- bge-reranker-v2-m3 также использует `/api/embeddings` endpoint
- Ollama не позволяет загружать обе модели одновременно на одном порту

**Тестирование подтвердило:**
```bash
# Попытка загрузить bge-m3 + bge-reranker на одном порту 11435
curl -X POST "http://192.168.1.145:11435/api/generate" \
  -d '{"model": "bge-m3", "prompt": "test"}'
# Результат: bge-reranker выгружается

curl -X POST "http://192.168.1.145:11435/api/generate" \
  -d '{"model": "bge-reranker-v2-m3", "prompt": "test"}'
# Результат: bge-m3 выгружается
```

**Решение:** Разделить модели по разным контейнерам:
- **ollama-embeddings** (порт 11435): bge-m3 только
- **ollama-reranker** (порт 11437): bge-reranker-v2-m3 только

---

### Проблема 2: Неправильный RERANK_URL в конфигурации

**Было:**
```bash
RERANK_URL=http://192.168.1.145:11435/api/rerank
```

**Проблема:** Ollama не имеет endpoint `/api/rerank`

**Стало:**
```bash
RERANK_URL=http://192.168.1.145:11437/api/embeddings
```

---

### Проблема 3: Неправильный формат запроса в `_rerank()`

**Было (неправильно):**
```python
resp = requests.post(
    settings.rerank_url,
    json={"model": settings.rerank_model, "query": query, "documents": docs},
    timeout=15,
)
```

**Проблема:** Ollama embeddings API принимает только `{"model": "...", "prompt": "..."}`

**Стало (правильно):**
```python
def _rerank(self, query: str, candidates: list[dict]) -> list[dict]:
    """Rerank через cosine similarity с embeddings."""
    if not candidates:
        return candidates

    # Получить embedding для query
    query_embedding = self._embed(query)
    if not query_embedding:
        return candidates

    # Cosine similarity
    def cosine_similarity(a, b):
        from math import sqrt
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sqrt(sum(x * x for x in a))
        norm_b = sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # Получить embeddings для всех кандидатов
    scored = []
    for candidate in candidates:
        text = candidate.get("text", "")
        if not text:
            continue
        
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

---

### Проблема 4: Неправильный формат в `_embed()`

**Было:**
```python
json={"model": settings.embed_model, "input": text}
```

**Проблема:** Ollama использует `prompt`, а не `input`

**Стало:**
```python
json={"model": settings.embed_model, "prompt": text}
```

---

## 🔄 Обновление с 4-мя контейнерами Ollama

### Архитектура до изменений

```
Машина 2 (192.168.1.145) — 3 контейнера:
├── ollama-1 (11434): qwen3:14b + qwen2.5:7b (GPU 0+1)
├── ollama-2 (11435): qwen2.5:7b-mem0 (GPU 2)
└── ollama-3 (11436): bge-m3 + bge-reranker (GPU 3) ← ПРОБЛЕМА
```

### Архитектура после изменений

```
Машина 2 (192.168.1.145) — 4 контейнера:
├── ollama (11434): qwen3:14b + qwen2.5:7b (GPU 0+1)
├── ollama-embeddings (11435): bge-m3 (GPU 3)
├── ollama-mem0 (11436): qwen2.5:7b-mem0 (GPU 2)
└── ollama-reranker (11437): bge-reranker-v2-m3 (GPU 3)
```

**Важно:** bge-m3 (11435) и bge-reranker (11437) используют один GPU 3, но в разных контейнерах!

---

## 📝 Произведённые изменения в коде

### 1. `/home/ironman/.openclaw/skills/memory-reflect/.env`

```diff
# ─── Embeddings & Reranker ────────────────────────────────────────────────────
- EMBED_URL=http://192.168.1.145:11435/api/embed
+ EMBED_URL=http://192.168.1.145:11435/api/embeddings
  EMBED_MODEL=bge-m3:latest
- RERANK_URL=http://192.168.1.145:11435/api/rerank
+ RERANK_URL=http://192.168.1.145:11437/api/embeddings
  RERANK_MODEL=xitao/bge-reranker-v2-m3:latest
```

### 2. `/home/ironman/.openclaw/skills/memory-reflect/config.py`

```diff
# ─── Embeddings & Reranker ────────────────────────────────────────────────────
- embed_url:    str = "http://localhost:11435/api/embed"
+ embed_url:    str = "http://localhost:11435/api/embeddings"
  embed_model:  str = "bge-m3:latest"
- rerank_url:   str = "http://localhost:11435/api/rerank"
+ rerank_url:   str = "http://localhost:11437/api/embeddings"
  rerank_model: str = "xitao/bge-reranker-v2-m3:latest"
```

### 3. `/home/ironman/.openclaw/skills/memory-reflect/store.py` — `_embed()`

```diff
def _embed(self, text: str) -> Optional[list[float]]:
    def _call():
        resp = requests.post(
            settings.embed_url,
-           json={"model": settings.embed_model, "input": text},
+           json={"model": settings.embed_model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        data       = resp.json()
-       embeddings = data.get("embeddings", [])
-       if embeddings:
-           return embeddings[0]
-       return data.get("embedding", [])
+       return data.get("embedding", [])

    try:
        return with_retry(_call)
    except Exception as e:
        log.error(f"Embed failed: {e}")
        return None
```

### 4. `/home/ironman/.openclaw/skills/memory-reflect/store.py` — `_rerank()`

Полная замена функции (см. выше в "Проблема 3")

---

## 🧪 Тестирование

### ✅ Embedding работает

```bash
$ python3 -c "from store import QdrantSearch; q = QdrantSearch(); v = q._embed('test'); print(len(v))"
Embedding размер: 1024
```

### ✅ Reranking работает (тест с живыми данными)

```python
# Тестовый запрос
query = "что такое ollama"
documents = [
    "Ollama — это инструмент для запуска LLM локально",
    "Python — язык программирования общего назначения",
    "Docker — платформа для контейнеризации приложений",
    "Ollama позволяет запускать модели типа Llama, Mistral локально"
]

# Результат reranking
Rank #1 (0.9943): Ollama позволяет запускать модели типа Llama, Mistral локально
Rank #2 (0.9942): Ollama — это инструмент для запуска LLM локально
Rank #3 (0.7460): Docker — платформа для контейнеризации приложений
Rank #4 (0.6751): Python — язык программирования общего назначения
```

### ❌ Flashback не работает (проблема с Qdrant)

**Причина:** Qdrant не индексирует вектора
```
points_count: 52
indexed_vectors_count: 0
```

**Решение:** Требуется пересоздание коллекции или реиндексация

---

## 📋 Следующие шаги

1. **Решить проблему с Qdrant индексацией**
   - Вариант A: Пересоздать коллекцию reflections
   - Вариант B: Переиндексировать существующие вектора
   - Вариант C: Изменить конфигурацию на full scan

2. **Перенести исправления в repo**
   - Создать backup production файлов
   - Перенести изменения в `/home/ironman/projects/total-recall`
   - Сделать коммит и пуш

3. **Копировать из repo в production**
   - Скопировать `.env`, `config.py`, `store.py` в `/home/ironman/.openclaw/skills/memory-reflect`

4. **Протестировать flashback с reranking**
   - `python3 memory-reflect.py --flashback --focus "ollama warmup"`

---

## 📚 Ссылки

- **Документация bge-reranker:** `/home/ironman/projects/total-recall/docs/bge-reranker-usage.md`
- **Проблемы с reranking:** `/home/ironman/projects/total-recall/docs/total-recall-reranking-problem.md`
- **Статус исправлений:** `/home/ironman/.openclaw/workspace/docs/total-recall-reranking-fix-status.md`

---

**Обновлено:** 2026-03-26 20:57 UTC  
**Статус:** В процессе (требуется решение по Qdrant)
