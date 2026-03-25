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
└── README.md             # Эта документация
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
