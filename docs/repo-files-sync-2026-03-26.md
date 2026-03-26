# Перенос production файлов в repo

**Дата:** 2026-03-26  
**Статус:** ✅ Выполнено

---

## Проблема

Production файлы редактировались напрямую в `/home/ironman/.openclaw/skills/memory-reflect/`:
- `.env` — конфигурация
- `config.py` — настройки
- `store.py` — код

Это нарушение процесса работы: файлы не в version control, нет истории изменений.

---

## Решение

### 1. Backup production файлов
```bash
/home/ironman/.openclaw/skills/memory-reflect/backup-20260326-231637/
```

### 2. Перенос в repo
Файлы перенесены в `/home/ironman/projects/total-recall/config/`:
- `.env.example` — шаблон конфигурации (не `.env` из-за .gitignore)
- `config.py` — настройки с исправленными дефолтными значениями
- `store.py` — код с исправленными функциями

### 3. Git commit
```
85e4159 config: добавить исправленные файлы total-recall после обновления reranking
```

---

## Изменения в файлах

### .env.example
```diff
- EMBED_URL=http://192.168.1.145:11435/api/embed
+ EMBED_URL=http://${IP_ADDRESS:-localhost}:11435/api/embeddings

- RERANK_URL=http://192.168.1.145:11435/api/rerank
+ RERANK_URL=http://${IP_ADDRESS:-localhost}:11437/api/embeddings

- SIMILARITY_THRESHOLD=0.72
+ SIMILARITY_THRESHOLD=0.40
```

### config.py
```diff
- embed_url:    str = "http://localhost:11435/api/embed"
+ embed_url:    str = "http://localhost:11435/api/embeddings"

- rerank_url:   str = "http://localhost:11435/api/rerank"
+ rerank_url:   str = "http://localhost:11437/api/embeddings"
```

### store.py
```diff
# _embed()
- json={"model": settings.embed_model, "input": text}
+ json={"model": settings.embed_model, "prompt": text}

# _rerank()
- Использовал query+documents формат
+ Использует cosine similarity с embeddings
```

---

## Процесс копирования в production

Для применения изменений из repo в production:

```bash
# 1. Сделать backup текущих production файлов
cp -r /home/ironman/.openclaw/skills/memory-reflect /home/ironman/.openclaw/skills/memory-reflect-backup-$(date +%Y%m%d)

# 2. Скопировать файлы из repo
cp /home/ironman/projects/total-recall/config/config.py /home/ironman/.openclaw/skills/memory-reflect/
cp /home/ironman/projects/total-recall/config/store.py /home/ironman/.openclaw/skills/memory-reflect/

# 3. Обновить .env (не .env.example!)
cp /home/ironman/projects/total-recall/config/.env.example /home/ironman/.openclaw/skills/memory-reflect/.env
# Заполнить переменные окружения:
# - IP_ADDRESS=192.168.1.145
# - NEO4J_PASSWORD=mem0graph
```

---

## Статус

✅ Backup создан  
✅ Файлы перенесены в repo  
✅ Git коммит выполнен  
✅ Запушено на GitHub  

**Git коммит:** `85e4159 config: добавить исправленные файлы total-recall после обновления reranking`

**GitHub:** https://github.com/zimaclaw/total-recall/tree/main/config

---

**Обновлено:** 2026-03-26 23:16 UTC
