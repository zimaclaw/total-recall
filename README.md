# Total Recall — OpenClaw Plugin

Auto flashback from memory-reflect before agent starts.

## Что делает

Перед каждым запуском агента:
1. Анализирует prompt и определяет категорию (infra, dev, deploy, memory, etc.)
2. Запускает `memory-reflect.py --flashback --category <category>`
3. Форматирует результат и inject в контекст через `prependContext`

## Структура

```
total-recall/
├── package.json          # Метаданные npm + openclaw extensions
├── index.js              # Регистрация hook before_agent_start
├── handler.js            # Логика: inferCategory → flashback → formatContext
├── openclaw.plugin.json  # UI hints и config schema
├── README.md             # Эта документация
└── reranker-api/         # Dedicated reranking API (опционально)
    ├── docker-compose.yml
    ├── Dockerfile
    ├── pyproject.toml     # Poetry dependencies
    ├── app.py             # FastAPI приложение
    └── README.md
```

## Установка

### Способ 1: Через openclaw.json

Добавить в `plugins.allow`:
```json
{
  "plugins": {
    "allow": ["total-recall", ...],
    "entries": {
      "total-recall": {
        "enabled": true,
        "config": {}
      }
    }
  }
}
```

Копировать в extensions:
```bash
cp -r ~/projects/total-recall /home/ironman/.openclaw/extensions/
```

### Способ 2: Символическая ссылка

```bash
ln -s ~/projects/total-recall /home/ironclaw/extensions/total-recall
```

Добавить в openclaw.json:
```json
{
  "plugins": {
    "installs": {
      "total-recall": {
        "source": "path",
        "sourcePath": "~/projects/total-recall",
        "installPath": "/home/ironman/.openclaw/extensions/total-recall"
      }
    }
  }
}
```

## Проверка

Перезапустить gateway:
```bash
openclaw gateway restart
```

Проверить логи:
```bash
tail -f /tmp/total-recall.log
```

Дать задачу про порты/деплой/систему — должен сработать flashback.

## Логика категоризации

Категории определяются по ключевым словам в prompt:

- **infra**: deploy, server, docker, nginx, port, network, сервер, порт, деплой, systemd
- **dev**: code, script, bug, fix, function, git, код, скрипт, баг, python, node
- **memory**: memory, remember, flashback, reflect, память, вспомни, принцип
- **research**: research, find, search, analyze, исследуй, найди, поищи
- **test**: test, check, validate, verify, тест, проверь
- **deploy**: deploy, release, publish, деплой, релиз
- **plan**: plan, schedule, roadmap, план, роадмап
- **write**: write, document, report, напиши, документ

Если ключевые слова не найдены — категория по умолчанию `dev`.

## Формат inject

```
=== MEMORY CONTEXT [category] ===
[lessons и принципы из flashback]
=== END MEMORY CONTEXT ===
```

## Отладка

Лог файл: `/tmp/total-recall.log`

Пример лога:
```
[2026-03-25T21:30:00.000Z] category=infra prompt="как настроить nginx reverse proxy"
[2026-03-25T21:30:01.500Z] injected 523 chars
```

---

## Reranker API (опционально)

### Исследование: почему bge-reranker-v2-m3 не работает в Ollama

**Проблема:**
- Ollama 0.17.6 не имеет `/api/rerank` endpoint
- Модель `bge-reranker-v2-m3` через `/api/generate` работает как генератор текста, а не как reranker
- PR #14172 "Add reranking support" ещё не merged в main branch

**Причина:**
- `bge-reranker-v2-m3` — это **encoder-only (BERT-based) sequence classifier**
- Принимает пару (query, document) и выводит **relevance score (логит)**
- Ollama пытается использовать модель как **decoder-only LLM** (next-token prediction)
- Нет endpoint для получения logits из `cls.output` слоя

**Текущее решение:**
- `store.py` использует **cosine similarity** с embeddings
- Это рабочее решение, но менее точное чем cross-encoder
- Cross-encoder обрабатывает query и passage вместе, понимает контекст

### Reranker API проект

**Цель:** Dedicated API сервис для reranking через cosine similarity

**Архитектура:**
```
memory-reflect (store.py)
  ↓ вызывает (опционально)
reranker-api (192.168.1.164:8081)
  ↓ вызывает
ollama-reranker (192.168.1.145:11437)
  └─ bge-reranker-v2-m3:latest
```

**Запуск:**
```bash
cd /home/ironman/projects/total-recall/reranker-api
docker-compose up --build
```

**API Contract:**
```bash
# Запрос
curl -X POST http://localhost:8081/rerank \
  -H "Content-Type: application/json" \
  -d '{
    "query": "what is panda?",
    "candidates": [
      {"text": "hi", "id": "conclusion_456", "category": "infra"},
      {"text": "The giant panda is a bear species", "id": "conclusion_123", "category": "test"}
    ]
  }'

# Ответ
{
  "model": "xitao/bge-reranker-v2-m3:latest (cosine similarity)",
  "results": [
    {"text": "The giant panda is a bear species", "id": "conclusion_123", "category": "test", "timestamp": 1703275200, "relevance_score": 0.85},
    {"text": "hi", "id": "conclusion_456", "category": "infra", "timestamp": 1703260800, "relevance_score": 0.12}
  ]
}
```

**Важно:** API принимает полные объекты candidates с metadata, а не только текст.

**Конфигурация:**
```bash
# .env в memory-reflect
RERANKER_API_URL=  # пустой = использовать локальный cosine similarity
# Или: RERANKER_API_URL=http://192.168.1.164:8081/rerank
```

---

## 🧠 Архитектура памяти (Memory Stack)

**Важно:** Два разных типа данных в двух разных хранилищах:

### **Neo4j (графовая база)**

Хранит **иерархию знаний**:
```
Conclusion (concrete facts)
  ↓ generalizes_to
Lesson (обобщённые уроки)
  ↓ abstracted_to
Principle (универсальные принципы)
  ↓ abstracted_to
Meta (метапринципы)
```

**Что хранится:**
- ✅ Conclusion, Lesson, Principle, Meta (все уровни)
- ✅ Графовые связи между уровнями
- ✅ Байесовское обновление confidence
- ✅ История применения уроков

**Для чего:**
- Рефлексия (Lesson → Principle → Meta)
- Навигация по иерархии знаний
- Отслеживание связей между концепциями

---

### **Qdrant (векторная база)**

Хранит **только concrete данные** для семантического поиска:
```
Conclusion (с embeddings)
Lesson (с embeddings, опционально)
```

**Что хранится:**
- ✅ Conclusion с векторными embeddings
- ⚠️ Lesson (опционально, через process_dump)
- ❌ Principle (НЕ хранятся!)
- ❌ Meta (НЕ хранятся!)

**Для чего:**
- Семантический поиск по тексту
- Flashback по query (конкретные вопросы)
- Reranking через cosine similarity

---

### **Почему разделение?**

1. **Principle/Meta не нужны в векторном поиске** — они абстрактные, не содержат конкретных фактов
2. **Граф важнее для абстракций** — иерархия Principle → Meta лучше представляется в Neo4j
3. **Concrete данные нужны для поиска** — conclusion содержат конкретные факты, которые можно найти через embeddings

---

### **Flashback с двумя источниками**

```python
def flashback_focus(query, category, limit):
    # 1. Concrete из Qdrant (векторный поиск)
    concrete = search_qdrant(query, category, levels=["conclusion", "lesson"])
    
    # 2. Abstract из Neo4j (категорийный поиск) — TODO
    abstract = search_neo4j(category, levels=["principle", "meta"])
    
    # 3. Комбинация с балансом 60/40
    return combine(concrete[:3], abstract[:2])
```

**Текущая реализация (v1.0.2):**
- ✅ Concrete из Qdrant (векторный поиск conclusion/lesson)
- ✅ Классификация query (concrete vs abstract)
- ✅ Критик для анализа релевантности
- ⏳ Abstract из Neo4j — TODO (требует интеграции Neo4jStore + QdrantSearch)

**Пример:**
- Query: "порт 8080 занят" → concrete (conclusion про порты)
- Query: "как принимать решения" → abstract (principle/meta про решения) — TODO

**Подробнее:** см. `/home/ironman/projects/total-recall/reranker-api/README.md`
