# Использование bge-reranker-v2-m3 в Ollama

## 📖 Обзор

**bge-reranker-v2-m3** — это multilingual reranker модель от BAAI (Beijing Academy of Artificial Intelligence).

### Ключевые особенности
- **Multilingual** — поддерживает множество языков
- **Lightweight** — легко развёртывается, быстрый inference
- **Base model:** bge-m3
- **Размер:** ~567M параметров (F16)
- **VRAM:** ~1.2GB

### 🔗 Ссылки
- **HuggingFace:** https://huggingface.co/BAAI/bge-reranker-v2-m3
- **FlagEmbedding:** https://github.com/FlagOpen/FlagEmbedding
- **Ollama model:** `xitao/bge-reranker-v2-m3:latest`

---

## 🚀 Использование через Ollama API

### Архитектура

```
Query + Documents → Embeddings API → Cosine Similarity → Reranked Results
```

**Важно:** В Ollama bge-reranker работает через `/api/embeddings`, а не как specialized reranker.

### Порт и модель
- **URL:** `http://192.168.1.145:11437/api/embeddings`
- **Model:** `xitao/bge-reranker-v2-m3:latest`
- **Context:** 4096
- **Embedding размер:** 1024

---

## 📝 Примеры

### 1. Получить embedding для текста

```bash
curl -X POST "http://192.168.1.145:11437/api/embeddings" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "xitao/bge-reranker-v2-m3:latest",
    "prompt": "что такое ollama"
  }'
```

**Ответ:**
```json
{
  "embedding": [-0.281, -0.255, -0.246, ...]  // 1024 значения
}
```

### 2. Reranking pipeline (Python)

```python
import requests
from math import sqrt

RERANKER_URL = "http://192.168.1.145:11437/api/embeddings"

# Запрос и документы
query = "что такое ollama"
documents = [
    "Ollama — это инструмент для запуска LLM локально",
    "Python — язык программирования общего назначения",
    "Docker — платформа для контейнеризации приложений",
    "Ollama позволяет запускать модели типа Llama, Mistral локально"
]

# Получить embedding для запроса
query_embedding = requests.post(RERANKER_URL, json={
    "model": "xitao/bge-reranker-v2-m3:latest",
    "prompt": query
}).json()["embedding"]

# Cosine similarity
def cosine_similarity(a, b):
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(x * x for x in b))
    return dot_product / (norm_a * norm_b)

# Rerank документы
results = []
for doc in documents:
    doc_embedding = requests.post(RERANKER_URL, json={
        "model": "xitao/bge-reranker-v2-m3:latest",
        "prompt": doc
    }).json()["embedding"]
    similarity = cosine_similarity(query_embedding, doc_embedding)
    results.append((doc, similarity))

# Сортировка по релевантности
results.sort(key=lambda x: x[1], reverse=True)

# Вывод
for rank, (doc, score) in enumerate(results, 1):
    print(f"Rank #{rank} (score: {score:.4f}): {doc}")
```

**Результат:**
```
Rank #1 (score: 0.9943): Ollama позволяет запускать модели типа Llama, Mistral локально
Rank #2 (score: 0.9942): Ollama — это инструмент для запуска LLM локально
Rank #3 (score: 0.7460): Docker — платформа для контейнеризации приложений
Rank #4 (score: 0.6751): Python — язык программирования общего назначения
```

---

## 🔧 Использование в total-recall / memory-reflect

### Flashback с reranking

```python
# 1. Получаем query embedding
query_embedding = get_embedding(query)

# 2. Получаем top-20 из Qdrant (semantic search)
candidates = qdrant.search(query_embedding, top_k=20)

# 3. Rerank через bge-reranker
reranked = []
for candidate in candidates:
    doc_embedding = get_embedding(candidate.text)
    score = cosine_similarity(query_embedding, doc_embedding)
    reranked.append((candidate, score))

# 4. Сортируем и берём top-5
reranked.sort(key=lambda x: x[1], reverse=True)
final_results = reranked[:5]
```

---

## ⚠️ Важные замечания

### Ollama vs FlagEmbedding

| Функция | FlagEmbedding | Ollama |
|---------|---------------|--------|
| **API** | Specialized reranker | Embeddings API |
| **Вход** | [query, passage] | prompt |
| **Выход** | Score (-∞, +∞) | Embedding (1024d) |
| **Normalisation** | Sigmoid (0-1) | Cosine similarity (-1, 1) |

### Рабочее решение в Ollama

1. Получаем embedding для query
2. Получаем embeddings для documents
3. Считаем cosine similarity
4. Сортируем по сходству

### Производительность

- **Embedding размер:** 1024 вектора
- **Скорость:** ~4ms на запрос (локально)
- **VRAM:** 1.2GB
- **keep_alive:** -1 (модель не выгружается)

---

## 📊 Метрики

### Тестовые результаты

**Запрос:** "что такое ollama"

| Документ | Сходство | Релевантность |
|----------|----------|---------------|
| Ollama позволяет запускать модели типа Llama, Mistral локально | 0.9943 | ✅ Высокая |
| Ollama — это инструмент для запуска LLM локально | 0.9942 | ✅ Высокая |
| Docker — платформа для контейнеризации приложений | 0.7460 | ⚠️ Средняя |
| Python — язык программирования общего назначения | 0.6751 | ❌ Низкая |

**Вывод:** bge-reranker-v2-m3 корректно ранжирует документы по релевантности к запросу.

---

## 🔄 Интеграция с OpenClaw

### В memory-reflect

```bash
# Flashback с reranking
cd /home/ironman/.openclaw/skills/memory-reflect
.venv/bin/python memory-reflect.py --flashback --focus "тема"
```

Reranking автоматически применяется к top-20 результатам из Qdrant, возвращая top-5.

### В Curator (Фаза 3)

```python
def get_flashback_context(query, category=None):
    # 1. Semantic search в Qdrant
    candidates = semantic_search(query, top_k=20)
    
    # 2. Rerank через bge-reranker
    reranked = rerank(query, candidates, top_k=5)
    
    # 3. Форматирование контекста
    context = format_context(reranked)
    
    return {"prependContext": context}
```

---

## 📚 Дополнительные ресурсы

- **FlagEmbedding docs:** https://github.com/FlagOpen/FlagEmbedding
- **BGE M3 paper:** https://arxiv.org/abs/2402.03216
- **Ollama API docs:** https://github.com/ollama/ollama/blob/main/docs/api.md

---

**Последнее обновление:** 2026-03-26 20:24 UTC  
**Статус:** ✅ Тестирование пройдено, готово к использованию
