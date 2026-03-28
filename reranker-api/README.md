# Reranker API (для total-recall)

Dedicated API сервис для reranking через cosine similarity с embeddings.

## Архитектура

```
ollama-reranker (192.168.1.145:11437)
  └─ bge-reranker-v2-m3:latest (F16)
  └─ /api/embeddings endpoint

reranker-api (192.168.1.164:8081)
  └─ FastAPI + Python + Poetry
  └─ Вызывает ollama-reranker для embeddings
  └─ Вычисляет cosine similarity
  └─ Возвращает sorted results с relevance scores
  └─ Кэширование embeddings (LRU, 1000 записей)
  └─ Batch processing (ограничение параллелизма)
  └─ API key аутентификация
  └─ Мониторинг и метрики
```

## Улучшения v1.0.1

✅ **Оптимизация входных данных** — только essential поля (text, id, _score)
✅ **Кэширование embeddings** — LRU cache, 1000 записей
✅ **Batch processing** — ограничение параллелизма (по умолчанию 5 запросов)
✅ **Мониторинг** — метрики через /health и /metrics
✅ **Аутентификация** — API key через header X-API-Key
✅ **Сохранение original_score** — для сравнения с новым score
✅ **Валидация данных** — Pydantic Field с min_length, max_length

## API Contract

### Запрос (POST /rerank)

**Headers:**
```
X-API-Key: secret-key-change-this
Content-Type: application/json
```

**Body (только essential поля):**
```json
{
  "query": "what is panda?",
  "candidates": [
    {
      "text": "hi",
      "id": "conclusion_456",
      "_score": 0.45
    },
    {
      "text": "The giant panda is a bear species endemic to China",
      "id": "conclusion_123",
      "_score": 0.75
    },
    {
      "text": "Pandas are native to China",
      "id": "conclusion_789",
      "_score": 0.65
    }
  ]
}
```

### Ответ

```json
{
  "model": "xitao/bge-reranker-v2-m3:latest (cosine similarity)",
  "results": [
    {
      "id": "conclusion_123",
      "text": "The giant panda is a bear species endemic to China",
      "relevance_score": 0.85,
      "original_score": 0.75
    },
    {
      "id": "conclusion_789",
      "text": "Pandas are native to China",
      "relevance_score": 0.72,
      "original_score": 0.65
    },
    {
      "id": "conclusion_456",
      "text": "hi",
      "relevance_score": 0.12,
      "original_score": 0.45
    }
  ]
}
```

**Примечание:** category и timestamp удалены из формата — только essential поля.

### Формат для memory-reflect

**Вход (candidates из store.py):**

```python
candidates = [
    {
        "text": "The giant panda is a bear species",
        "id": "conclusion_123",
        "category": "test",
        "timestamp": 1703275200,
        "_score": 0.75  # из vector search
    },
    {
        "text": "hi",
        "id": "conclusion_456",
        "category": "infra",
        "timestamp": 1703260800,
        "_score": 0.45
    }
]
```

**Выход (после _rerank или API):**

```python
scored_candidates = [
    {
        "text": "The giant panda is a bear species",
        "id": "conclusion_123",
        "category": "test",
        "timestamp": 1703275200,
        "_score": 0.85  # обновлённый relevance score
    },
    {
        "text": "hi",
        "id": "conclusion_456",
        "category": "infra",
        "timestamp": 1703260800,
        "_score": 0.12
    }
]
# Отсортировано по _score descending
# Вся metadata сохранена!
```

## Зачем это нужно?

Ollama 0.17.6 не имеет `/api/rerank` endpoint. Модель `bge-reranker-v2-m3` через `/api/generate` работает как генератор текста, а не как reranker.

Это решение:
- ✅ Использует ollama-reranker для embeddings
- ✅ Вычисляет cosine similarity (как FlagEmbedding)
- ✅ Возвращает scores в формате как у dedicated reranker
- ✅ Не требует Gateway для этой задачи

## Запуск

### 1. Build и run через docker-compose

```bash
cd /home/ironman/projects/total-recall/reranker-api
docker-compose up --build
```

### 2. Проверка health

```bash
curl http://localhost:8081/health
```

### 3. Тестовый запрос

```bash
curl -X POST http://localhost:8081/rerank \
  -H "X-API-Key: secret-key-change-this" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "what is panda?",
    "candidates": [
      {"text": "hi", "id": "conclusion_456", "_score": 0.45},
      {"text": "The giant panda is a bear species", "id": "conclusion_123", "_score": 0.75},
      {"text": "Pandas are native to China", "id": "conclusion_789", "_score": 0.65}
    ]
  }'
```

### 4. Проверка метрик

```bash
# Health check с метриками
curl http://localhost:8081/health

# Детальные метрики
curl http://localhost:8081/metrics
```

**Пример ответа /metrics:**
```json
{
  "requests": {
    "total": 100,
    "errors": 2,
    "error_rate": 0.02
  },
  "performance": {
    "avg_rerank_time_sec": 0.45,
    "total_rerank_time_sec": 45.2
  },
  "cache": {
    "hits": 23,
    "misses": 77,
    "size": 77,
    "maxsize": 1000,
    "hit_rate": 0.23
  }
}
```

## Конфигурация

**Переменные окружения:**

```bash
# .env для docker-compose
OLLAMA_RERANKER_URL=http://192.168.1.145:11437
RERANKER_API_KEY=your-secret-api-key-here
RERANKER_BATCH_SIZE=5  # параллельных запросов к Ollama
```

**В docker-compose.yml:**
```yaml
environment:
  - OLLAMA_RERANKER_URL=http://192.168.1.145:11437
  - RERANKER_API_KEY=your-secret-api-key-here
  - RERANKER_BATCH_SIZE=5
```

**Важно:** Измените `RERANKER_API_KEY` на безопасное значение!

## Интеграция с memory-reflect

**Важно:** По умолчанию используется локальный cosine similarity (в `store.py`).
Dedicated API используется только если `reranker_api_url` задан в `.env`.

Пример `.env` для memory-reflect:

```bash
RERANKER_API_URL=http://192.168.1.164:8081/rerank
RERANKER_API_KEY=your-secret-api-key-here
```

Если поле пустое или не задано — используется локальный cosine similarity (текущая реализация).

## Ресурсы

- **CPU:** минимальный (cosine similarity — простые вычисления)
- **RAM:** ~100-200MB (Python + FastAPI + Poetry)
- **GPU:** не требуется (модель в ollama-reranker)
- **Network:** localhost или LAN (192.168.1.x)
- **Порт:** 8081 (изменён с 8080)

## Future improvements

1. Добавить caching для embeddings
2. Поддержка batch processing
3. Добавить specialized reranker (FlagEmbedding) как fallback
4. Поддержка Ollama `/api/rerank` когда будет доступен

## Структура проекта

```
/home/ironman/projects/total-recall/
├── reranker-api/
│   ├── docker-compose.yml      ← конфигурация Docker
│   ├── Dockerfile              ← build с Poetry
│   ├── pyproject.toml          ← Poetry dependencies
│   ├── app.py                  ← FastAPI приложение
│   └── README.md               ← эта документация
├── ... (остальные файлы total-recall)
```
