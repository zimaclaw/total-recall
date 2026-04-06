# Промпт для Пятницы — коммит total-recall v0.2.0

Пятница, нужно закоммитить изменения в репо `~/projects/total-recall`.

## Что изменилось

### Новые файлы
- `docs/integration.md` — документация по интеграции plugin с OpenClaw
- `docs/decisions.md` — зафиксированные архитектурные решения
- `docs/session-id-research.md` — исследование session ID в OpenClaw
- `docs/session-store-research.md` — исследование session store и .jsonl файлов

### Изменённые файлы
- `handler.js` — переписан: Curator v2, session store, правильные хуки
- `index.js` — обновлён: регистрация новых хуков (message_received, before_prompt_build, message_sent, command:new)

### Новые skills (в ~/.openclaw/skills/memory-reflect/)
- `session_store.py` — CLI для работы с PostgreSQL session store
- `kb_store.py` — CLI для Knowledge Base
- `migrate_summary.py` — Миграции

## Действия

1. Скопируй docs файлы из claude.ai в репо:
   - Они уже должны быть в `~/projects/total-recall/docs/` — проверь

2. Убедись что `handler.js` и `index.js` актуальны:
   ```bash
   diff ~/projects/total-recall/handler.js ~/.openclaw/extensions/total-recall/handler.js
   diff ~/projects/total-recall/index.js ~/.openclaw/extensions/total-recall/index.js
   ```
   Если есть diff — скопируй из extensions в repo (extensions — рабочая версия).

3. Синхронизируй memory-reflect:
   ```bash
   cp ~/.openclaw/skills/memory-reflect/session_store.py ~/projects/total-recall/skills/memory-reflect/
   cp ~/.openclaw/skills/memory-reflect/kb_store.py ~/projects/total-recall/skills/memory-reflect/
   cp ~/.openclaw/skills/memory-reflect/migrate_summary.py ~/projects/total-recall/skills/memory-reflect/
   ```

4. Создай коммит:
   ```bash
   cd ~/projects/total-recall
   git add -A
   git status  # проверь что добавилось
   git commit -m "feat: Curator v2 — session store, before_prompt_build, docs

   - Replace before_agent_start with before_prompt_build (correct hook)
   - Add message_received hook for writing clean prompts to PostgreSQL
   - Add message_sent hook for writing assistant replies
   - Add command:new hook for session boundary
   - Resolve sessionId via sessions.json (workaround for OpenClaw limitation)
   - Add session_store.py: skeleton and focus from PostgreSQL + pgvector
   - Remove curator file hook (conflicted with plugin)
   - Add docs: integration, decisions, research reports
   - Remove allowPromptInjection (does not exist in openclaw.json schema)"
   ```

5. Проверь что коммит чистый:
   ```bash
   git log --oneline -3
   git show --stat HEAD
   ```

Результат сообщи в чат.
