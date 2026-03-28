# Event Structure — before_agent_start hook

**Дата:** 2026-03-26 23:28 UTC  
**Плагин:** total-recall  
**Hook:** before_agent_start

---

## Структура event объекта

### Верхний уровень

| Ключ | Тип | Описание |
|------|-----|----------|
| `prompt` | string | Текст запроса пользователя |

### Полный event

```json
{
  "prompt": "<текст запроса пользователя>"
}
```

---

## Искомые поля

### ❌ Не найдено

- **sessionFile** — отсутствует
- **agentId** — отсутствует
- **messages** — отсутствует
- **contextTokens** — отсутствует
- **history** — отсутствует

### ✅ Найдено

- **prompt** — полный текст запроса пользователя (string)

---

## API доступные в hook

В hook доступны следующие методы api:

```javascript
api = {
  id, name, version, description, source, config, pluginConfig, runtime,
  logger, registerTool, registerHook, registerHttpHandler, registerHttpRoute,
  registerChannel, registerProvider, registerGatewayMethod, registerCli,
  registerService, registerCommand, resolvePath, on
}
```

---

## Выводы

1. **before_agent_start hook получает минимальный event** — только `prompt`
2. **Нет доступа к session данным** — sessionFile, agentId, messages не передаются
3. **Нет доступа к контексту** — contextTokens, history не доступны
4. **API доступен** — можно использовать api.logger, api.config и т.д.

---

## Рекомендации

Для доступа к session данным нужно:
1. Использовать другой hook (если есть)
2. Или получить данные через api (если доступно)
3. Или использовать beforePromptBuild вместо before_agent_start (проверить документацию)

---

**Тест выполнен:** 2026-03-26 23:28 UTC  
**Debug log:** /tmp/total-recall-debug.log
