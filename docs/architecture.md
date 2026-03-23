# Total Recall — архитектура системы памяти

## Цель

Система памяти и самообучения для персонального ассистента Friday на локальных LLM.
Агент накапливает опыт, учится на ошибках, не повторяет их.

---

## Стек

```
.181:8180    llamacpp    Qwen3.5-27B Q4_K_XL    Friday (основной агент)
.145:11434   ollama      qwen3:14b              субагенты
.145:11435   ollama      bge-m3:latest          embeddings
             ollama      qwen3:4b-mem0          mem0 extraction
.145:6333    Qdrant                             векторный поиск
.145:7687    Neo4j                              граф знаний
```

---

## Два слоя памяти

### Слой 1: mem0 (факты о пользователе)

- Коллекция `memories` в Qdrant
- `autoCapture: true` — автоматически захватывает факты из разговора
- `autoRecall: true` — автоматически вставляет релевантное в контекст
- `enableGraph: false` — Neo4j отключён для mem0

### Слой 2: total-recall (опыт агента)

- Коллекция `reflections` в Qdrant — семантический поиск выводов
- Neo4j — граф знаний под прямым управлением рефлектора
- Скрипт `memory-reflect.py` — асинхронный, запускается после каждой задачи

---

## Схема Neo4j

### Узлы

| Узел | Назначение |
|------|-----------|
| `Task` | Единица опыта — что делали, чем закончилось |
| `Evidence` | Факт — почему такой результат |
| `Conclusion` | Вывод — что делать иначе |
| `Lesson` | Принцип — обобщение из нескольких выводов |
| `Unknown` | Пробел в знаниях |
| `Event` | Исторический факт — вне Байеса |

### Рёбра

```
Task      -[HAS_EVIDENCE]->   Evidence
Task      -[HAS_CONCLUSION]-> Conclusion
Task      -[HAS_UNKNOWN]->    Unknown
Task      -[RELATED_TO]->     Task
Task      -[APPLIED_LESSON]-> Lesson
Iteration -[HAS_EVIDENCE]->   Evidence
Evidence  -[SUPPORTS]->       Conclusion
Evidence  -[REFUTES]->        Conclusion
Conclusion-[GENERALIZES_TO]-> Lesson
Conclusion-[INTERPRETS]->     Event
```

---

## Байесовское обновление confidence

### Два независимых параметра

```
evidence_type → prior + decay (для legal и knowledge)
category      → decay (для всех остальных)
```

### Prior по evidence_type

| evidence_type | prior | описание |
|---------------|-------|----------|
| `legal` | 0.85 | закон, норматив |
| `empirical` | 0.75 | проверил сам |
| `documented` | 0.65 | официальная документация |
| `knowledge` | 0.60 | фундаментальное знание |
| `interpretation` | 0.45 | трактовка |
| `inferred` | 0.45 | вывод из наблюдений |
| `generated` | 0.25 | рассуждение модели |

### Decay по категории (half-life)

| категория | half-life |
|-----------|-----------|
| `rules` | 3 года |
| `knowledge` | 6 лет |
| `legal` | 1 год |
| `memory` | 2 года |
| `infra` | 1.5 года |
| `deploy`, `plan`, `write`, `user` | 1 год |
| `dev`, `test` | 9 месяцев |
| `research` | 6 месяцев |

### Формула

```python
posterior = (prior * P_E_H) / (prior * P_E_H + (1-prior) * P_E_nH)

# Веса:
# cross-category confirm: +50%
# cross-category refute:  +100%
# refute weight по evidence_type: legal/knowledge 2.5x, empirical/documented 1.5x
```

---

## Два измерения Lesson

```
confidence — насколько принцип верен     (из Evidence → Conclusion → Байес)
mastery    — насколько агент его применяет (из APPLIED_LESSON рёбер)
```

| ситуация | значение |
|----------|----------|
| confidence высокий, mastery низкий | знает но не применяет → добавить в PROTOCOL.md |
| mastery высокий, confidence падает | применяет уверенно но принцип устарел → needs_review |

---

## Дамп задачи (5 полей)

```json
{
    "task_id":       "uuid",
    "goal":          "что хотели сделать",
    "outcome":       "success|fail|partial|abandoned",
    "reason":        "почему такой результат",
    "insight":       "что делать иначе в следующий раз",
    "evidence_type": "empirical|documented|legal|knowledge|inferred|generated",
    "ts":            1742000000,
    "lessons_applied": [
        {"principle": "текст урока", "helped": true}
    ]
}
```

---

## Flashback

```bash
poetry run python memory-reflect.py --flashback --category <категория>
```

Возвращает до 8 записей:
- 5 `Conclusion` из той же категории с `confidence ≥ 0.6`
- 3 `Lesson` с `confidence ≥ 0.75` и `mastery ≥ 0.6`

---

## Категории задач

```
dev, test, deploy, research, plan, write, memory, infra, user, rules, knowledge
```

Определяются автоматически скриптом по ключевым словам из `goal` и `reason`.

---

## Протокол для Friday

1. **Перед изменением системы** → flashback
2. **После завершённой задачи** → создать дамп → запустить рефлектор в фоне
3. **Задача не закрыта** пока не создан дамп

---

## Known issues

- `qwen3:4b-mem0` обрезает JSON при большом количестве воспоминаний в `memories`
- Коллекция `memories` содержит дубли и противоречия — требует очистки
- Семантический порог `0.72` иногда пропускает похожие выводы — fallback через keyword

---

## Следующие шаги

- [ ] Hook для автоматического inject из `reflections` при входящем сообщении
- [ ] Watchdog — healthcheck и откат
- [ ] Очистка коллекции `memories` от дублей
- [ ] Автоматическое обновление `PROTOCOL.md` когда `Lesson.confidence ≥ 0.9`
