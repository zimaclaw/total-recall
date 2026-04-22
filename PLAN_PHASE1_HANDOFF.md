# PLAN.md — Фаза 1: Ручной handoff для KB

**Дата:** 2026-04-22  
**Статус:** ✅ Миграция конфигурации выполнена, готово к реализации Фазы 1  
**Приоритет:** P1  
**Бюджет:** 4 часа (миграция конфига выполнена дополнительно)

---

## ✅ Выполнено (2026-04-22)

### Миграция конфигурации

**Цель:** Перенести конфигурацию total-recall из .env и хардкода в openclaw.json

**Результат:** ✅ Выполнено

**Изменения:**
1. **Schema обновлена** (`openclaw.plugin.json`):
   - Добавлены секции: debug, paths, kb, curator, embeddings, llm, postgresql, handoff
   - additionalProperties: false (строгая валидация)

2. **Конфиг применён** (`openclaw.json`):
   ```json
   "total-recall": {
     "enabled": true,
     "config": {
       "debug": true,
       "paths": { "log": "/tmp/total-recall.log", "memoryReflect": "...", "coreMd": "..." },
       "kb": { "scoreThreshold": 0.55, "maxResults": 3 },
       "curator": { "defaultContext": 32000 },
       "embeddings": { "url": "http://192.168.1.145:11435", "model": "bge-m3", "timeout": 30000 },
       "llm": { "provider": "ollama", "url": "http://192.168.1.145:11434", "model": "qwen3.5:27b-q4_K_M-N2", "timeout": 60000 },
       "postgresql": { "host": "192.168.1.145", "port": 5432, "database": "openclaw" },
       "handoff": { "maxMessages": 20, "similarityThreshold": 0.3, "minMessagesForSemantic": 15, "alwaysIncludeLast": 5 }
     }
   }
   ```

3. **handler.js обновлён**:
   - `getTotalRecallConfig()` для чтения из openclaw.json
   - Заменены хардкод пути на конфиг с fallback (TR_CONFIG.paths?.log || env || hardcode)
   - Обновлена `getKB()` с использованием `TR_CONFIG.kb`
   - Обновлён DEBUG режим (TR_CONFIG.debug ?? env)

4. **Git commit + push**:
   - `5bca23d` feat: чтение конфига из openclaw.json
   - `7117c65` fix: порядок объявления TR_CONFIG

5. **Деплой выполнен**:
   - handler.js скопирован в систему
   - Gateway перезапущен (pid 148062)
   - Плагин total-recall успешно инициализирован

**Ошибки и решения:**
- ❌ config.patch провалился (schema не поддерживала произвольные поля) → ✅ обновлена openclaw.plugin.json
- ❌ ReferenceError: Cannot access 'TR_CONFIG' before initialization → ✅ перемещено объявление TR_CONFIG выше зависимых констант

**Метрики:**
- Время миграции: ~90 минут
- Downtime gateway: ~30 секунд
- Ошибки в логе: 0 (после исправлений)
- Backup создан: ✅
- Git commit выполнен: ✅
- Post-check пройден: ✅

---

---

## 🎯 Цель

Реализовать команду `/kb handoff` для ручного сохранения текущей сессии в Knowledge Base.

---

## 📋 Задачи

### Задача 1.1: Команда `/kb handoff` — 3 часа

#### Шаг 1.1.1: Парсинг аргументов (30 мин)

**Файл:** `handler.js`  
**Место:** `onMessageReceived`

```javascript
// Проверка команды
if (text.startsWith('/kb handoff')) {
  const args = text.substring(11).trim();
  
  // Проверка флага --new-session
  const newSession = args.includes('--new-session');
  
  // Извлечение description
  let description = args;
  if (newSession) {
    description = args.replace('--new-session', '').trim();
  }
  description = description.trim().replace(/^["']|["']$/g, '');
  
  // Валидация
  if (!description || description.length < 5) {
    return res({
      text: "❌ Укажи краткое описание результата (минимум 5 символов)\n\nПример:\n/kb handoff \"Решена проблема с портом 8080\""
    });
  }
  
  // Обработка
  handleKbHandoff(sessionId, description, newSession);
}
```

**Требования:**
- Поддержка кавычек в description
- Флаг `--new-session` в любом месте
- Минимальная валидация (5 символов)

---

#### Шаг 1.1.2: Сбор контекста сессии (45 мин)

**Файл:** `handler.js`  
**Новая функция:** `collectSessionContext(sessionId, maxMessages)`

```javascript
async function collectSessionContext(sessionId, maxMessages = 20) {
  // SQL запрос последних N пар
  const query = `
    SELECT 
      sm.content as user_content,
      sa.content as assistant_content
    FROM session_messages sm
    LEFT JOIN session_messages sa 
      ON sa.session_id = sm.session_id 
      AND sa.turn_id = sm.turn_id + 1
    WHERE sm.session_id = $1
      AND sm.role = 'user'
    ORDER BY sm.created_at DESC
    LIMIT $2
  `;
  
  const rows = await pg.query(query, [sessionId, maxMessages]);
  
  // Форматирование контекста
  const context = rows.rows
    .reverse() // Восстанавливаем хронологический порядок
    .map(row => {
      return `User: ${row.user_content}\nAssistant: ${row.assistant_content || '(нет ответа)'}`;
    })
    .join('\n\n---\n\n');
  
  return context;
}
```

**Требования:**
- Максимум 20 сообщений (из конфига)
- Хронологический порядок
- Обработка отсутствующих ответов ассистента

---

#### Шаг 1.1.3: Создание KB записи (60 мин)

**Файл:** `handler.js`  
**Новая функция:** `createKbRecord(description, context)`

```javascript
async function createKbRecord(description, context) {
  // Генерация summary через LLM
  const summaryPrompt = `
Создай краткое summary (~200 токенов) для записи в Knowledge Base.

Описание пользователя: ${description}

Контекст сессии:
${context}

Возврати только текст summary без дополнительных комментариев.
  `;
  
  const summary = await callLLM(summaryPrompt, { maxTokens: 200 });
  
  // Вызов kb_store.py
  const { exec } = require('child_process');
  const skillsPath = '/home/ironman/.openclaw/skills/memory-reflect';
  
  return new Promise((resolve, reject) => {
    const command = `.venv/bin/python kb_store.py kb_save ` +
      `--title "${escapeShell(description)}" ` +
      `--summary "${escapeShell(summary)}" ` +
      `--content "${escapeShell(context)}" ` +
      `--source-tool "handoff" ` +
      `--category "inferred"`;
    
    exec(`cd ${skillsPath} && ${command}`, { encoding: 'utf8' }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`kb_store.py error: ${stderr}`));
        return;
      }
      
      // Парсинг ответа для получения kb_id
      try {
        const result = JSON.parse(stdout);
        resolve(result.kb_id);
      } catch (e) {
        resolve(null); // Fallback если kb_id не возвращён
      }
    });
  });
}
```

**Требования:**
- Summary через LLM (~200 токенов)
- Вызов kb_store.py через exec
- Обработка ошибок
- Извлечение kb_id из ответа

---

#### Шаг 1.1.4: Создание mapping записи (30 мин)

**Файл:** `handler.js`  
**Новая функция:** `createKbMapping(sessionId, kbId)`

```javascript
async function createKbMapping(sessionId, kbId) {
  const query = `
    INSERT INTO kb_session_mapping (session_id, kb_id, source_type)
    VALUES ($1, $2, 'handoff')
    ON CONFLICT DO NOTHING
  `;
  
  await pg.query(query, [sessionId, kbId]);
}
```

**Требования:**
- INSERT с ON CONFLICT DO NOTHING
- source_type = 'handoff'
- Транзакция с созданием KB записи

---

#### Шаг 1.1.5: Главная функция handoff (15 мин)

**Файл:** `handler.js`  
**Новая функция:** `handleKbHandoff(sessionId, description, newSession)`

```javascript
async function handleKbHandoff(sessionId, description, newSession) {
  try {
    // 1. Сбор контекста
    const context = await collectSessionContext(sessionId, 20);
    
    // 2. Создание KB записи
    const kbId = await createKbRecord(description, context);
    
    // 3. Создание mapping
    if (kbId) {
      await createKbMapping(sessionId, kbId);
    }
    
    // 4. Ответ пользователю
    let response = "✅ Сессия сохранена в Knowledge Base\n";
    response += `📝 Описание: ${description}\n`;
    response += `🔗 KB ID: ${kbId || 'N/A'}`;
    
    // 5. Опционально: новая сессия
    if (newSession) {
      // Очистка контекста или создание новой сессии
      response += "\n\n🆕 Новая сессия начата";
      // TODO: логика новой сессии
    }
    
    return res({ text: response });
    
  } catch (error) {
    console.error('KB handoff error:', error);
    return res({ 
      text: `❌ Ошибка при сохранении в KB: ${error.message}` 
    });
  }
}
```

**Требования:**
- Обёртка всех шагов
- Обработка ошибок
- Ответ пользователю
- Поддержка --new-session

---

### Задача 1.2: Таблица `kb_session_mapping` — 1 час

#### Шаг 1.2.1: SQL миграция (30 мин)

**Файл:** `migration_kb_session_mapping.sql`

```sql
-- Создание таблицы
CREATE TABLE IF NOT EXISTS kb_session_mapping (
  id SERIAL PRIMARY KEY,
  session_id UUID NOT NULL,
  kb_id UUID NOT NULL,
  source_type VARCHAR(20) NOT NULL CHECK (source_type IN ('handoff', 'cron')),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  CONSTRAINT fk_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
  CONSTRAINT unique_session_kb UNIQUE (session_id, kb_id)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_kb_session_mapping_session_id 
  ON kb_session_mapping(session_id);

CREATE INDEX IF NOT EXISTS idx_kb_session_mapping_kb_id 
  ON kb_session_mapping(kb_id);

CREATE INDEX IF NOT EXISTS idx_kb_session_mapping_source_type 
  ON kb_session_mapping(source_type);

-- Комментарии
COMMENT ON TABLE kb_session_mapping IS 'Маппинг сессий на KB записи';
COMMENT ON COLUMN kb_session_mapping.source_type IS 'Источник: handoff (ручной) или cron (авто)';
```

**Требования:**
- FOREIGN KEY с ON DELETE CASCADE
- UNIQUE constraint на (session_id, kb_id)
- Индексы для производительности

---

#### Шаг 1.2.2: Применение миграции (15 мин)

```bash
# Подключение к PostgreSQL
psql -h 192.168.1.145 -p 5432 -U openclaw -d openclaw < migration_kb_session_mapping.sql

# Проверка
psql -h 192.168.1.145 -p 5432 -U openclaw -d openclaw -c "\dt kb_session_mapping"
psql -h 192.168.1.145 -p 5432 -U openclaw -d openclaw -c "\di idx_kb_session*"
```

---

#### Шаг 1.2.3: Тесты (15 мин)

```sql
-- Тестовая вставка
INSERT INTO kb_session_mapping (session_id, kb_id, source_type)
VALUES ('test-uuid-here', 'kb-uuid-here', 'handoff');

-- Проверка индексов
EXPLAIN ANALYZE SELECT * FROM kb_session_mapping WHERE session_id = 'test-uuid-here';

-- Очистка
DELETE FROM kb_session_mapping WHERE session_id = 'test-uuid-here';
```

---

## 📁 Изменяемые файлы

1. **`~/projects/total-recall/handler.js`**
   - `onMessageReceived`: парсинг `/kb handoff`
   - `collectSessionContext()`: сбор контекста
   - `createKbRecord()`: создание KB записи
   - `createKbMapping()`: создание mapping
   - `handleKbHandoff()`: главная функция

2. **`~/projects/total-recall/migrations/migration_kb_session_mapping.sql`**
   - Новая миграция PostgreSQL

3. **`~/projects/total-recall/skills/memory-reflect/kb_store.py`**
   - Проверка поддержки `--source-tool handoff`
   - Проверка возврата kb_id

---

## 🧪 Тесты

### Тест 1: Базовый handoff

```bash
/kb handoff "Решена проблема с портом 8080"
```

**Ожидаемый результат:**
- KB запись создана
- mapping создан
- Ответ пользователю с KB ID

---

### Тест 2: Handoff с --new-session

```bash
/kb handoff --new-session "Завершена настройка ollama"
```

**Ожидаемый результат:**
- KB запись создана
- mapping создан
- Новая сессия начата (TODO)

---

### Тест 3: Валидация

```bash
/kb handoff "abc"
```

**Ожидаемый результат:**
- Ошибка: минимум 5 символов

---

## ⚠️ Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| kb_store.py не возвращает kb_id | Средняя | Среднее | Fallback: продолжить без kb_id |
| Контекст слишком длинный (>40K) | Низкая | Высокое | Ограничение maxMessages |
| PostgreSQL недоступен | Низкая | Высокое | Обработка ошибки, логирование |
| LLM timeout при генерации summary | Средняя | Низкое | Timeout 30s, fallback на description |

---

## 📊 Метрики успеха

- ✅ Команда `/kb handoff` работает
- ✅ KB запись создана (проверка через pg)
- ✅ mapping создан (проверка через pg)
- ✅ Ответ пользователю корректный
- ✅ Ошибки обрабатываются gracefully

---

## 🔄 Следующие шаги

1. **Критика плана** (Shifu)
2. **Уточнение деталей** (Oleg)
3. **Реализация** (Friday)
4. **Тесты** (Friday)
5. **Деплой** (Friday)

---

*Created: 2026-04-22 07:59 UTC*  
*Status: Draft for Shifu criticism*
