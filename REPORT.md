# Total Recall Plugin — Отчёт

## Шаг 1 — Изучение структуры plugins

### Найдено:

**Структура package.json плагина:**
```json
{
  "name": "@tensakulabs/openclaw-mem0",
  "version": "1.0.5",
  "type": "module",
  "main": "index.ts",
  "files": ["index.ts", "vendor/", "openclaw.plugin.json"],
  "openclaw": {
    "extensions": ["./index.ts"]
  }
}
```

**Как plugin регистрирует hooks:**
```javascript
export default function register(api) {
  // ...
  api.on('before_agent_start', async (event, ctx) => {
    // логика
    return { prependContext: '...' };
  });
}
```

**Где должен лежать plugin:**
- Физический путь: `/home/ironman/.openclaw/extensions/<plugin-name>/`
- Регистрация: `/home/ironman/.openclaw/openclaw.json` → `plugins.allow` + `plugins.entries`

### Ключевые моменты:
1. Плагин mem0 использует `api.on("before_agent_start", ...)` для inject контекста
2. Возвращает `{ prependContext: '...' }` — этот текст добавляется перед prompt
3. Конфиг плагина берётся из `openclaw.json` → `plugins.entries.<name>.config`

---

## Шаг 2 — Создание репозитория

✅ Репозиторий создан: `~/projects/total-recall`
✅ Ветка разработки: `feature/before-prompt-build`
✅ Структура файлов:
```
total-recall/
├── package.json          # npm metadata + openclaw extensions
├── index.js              # регистрация hook before_agent_start
├── handler.js            # логика: flashback → formatContext → return prependContext
├── openclaw.plugin.json  # UI hints и config schema
└── README.md             # документация
```

---

## Шаг 3 — Создание plugin

✅ Минимальный plugin создан на основе mem0:

**index.js:**
- Регистрация через `export default function register(api)`
- Hook `before_agent_start` вызывает `beforePromptBuild(event)`
- Возвращает `{ prependContext }`

**handler.js:**
- `inferCategory(prompt)` — определяет категорию по ключевым словам
- `runFlashback(category)` — запускает `memory-reflect.py --flashback --category`
- `formatContext(raw, category)` — форматирует вывод в `=== MEMORY CONTEXT ===`

---

## Шаг 4 — Установка plugin

✅ Плагин скопирован: `/home/ironman/.openclaw/extensions/total-recall/`
✅ Добавлен в `openclaw.json`:
- `plugins.allow`: добавлен `"total-recall"`
- `plugins.entries.total-recall`: `{"enabled": true, "config": {}}`
- `plugins.installs.total-recall`: metadata установки

✅ Gateway перезагружен

---

## Шаг 5 — Проверка

### Статус:
✅ **Plugin работает!**

### Тест 1: "какой порт использует qdrant"
- Категория: `dev`
- Flashback: ✅ загрузил 8 уроков
- MEMORY CONTEXT: ✅ виден в промпте
- Ответ: Qdrant использует порт 6333

### Тест 2: "еще тест"
- Категория: `test`
- Flashback: ✅ загрузил уроки про healthcheck и backup/restore
- MEMORY CONTEXT: ✅ виден в промпте

### Проблема решена:
Изначально плагин был отключён из-за конфликта с memory slot (`kind: "memory"`).
**Решение:** изменил `openclaw.plugin.json` → `"kind": "extension"`

---

## Результат

| Пункт | Статус |
|-------|--------|
| Структура plugin'а изучена | ✅ |
| Репозиторий создан | ✅ |
| Plugin минимальный создан | ✅ |
| Plugin установлен | ✅ |
| Gateway перезагружен | ✅ |
| before_prompt_build работает | ✅ |
| Пятница видит MEMORY CONTEXT | ✅ |
| Тест 1 (dev) | ✅ |
| Тест 2 (test) | ✅ |

---

## Следующие шаги

1. ✅ Закоммитить изменения в `feature/before-prompt-build`
2. ✅ Merge в `main`
3. ✅ Tag `v0.1.0`
4. Обновить README.md с инструкциями по установке

---

**Дата:** 2026-03-25 21:20 UTC
**Ветка:** `feature/before-prompt-build`
**Путь:** `~/projects/total-recall/REPORT.md`
