# Phi4-Mini Load Test — 2026-03-27

**Дата:** 2026-03-27 00:51 UTC  
**Цель:** Заменить qwen2.5:7b на phi4-mini в ollama (192.168.1.145:11434)

---

## Шаг 1: Выгрузка qwen2.5:7b

```bash
curl http://192.168.1.145:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen2.5:7b", "keep_alive": 0}'
```

**Результат:**
```json
{
  "model": "qwen2.5:7b",
  "created_at": "2026-03-27T00:51:36.299164349Z",
  "response": "",
  "done": true,
  "done_reason": "unload"
}
```

✅ **Статус:** Модель успешно выгружена (`done_reason: "unload"`)

---

## Шаг 2: Проверка статуса моделей (после выгрузки)

```bash
curl http://192.168.1.145:11434/api/ps
```

**Результат:**
```json
{
  "models": [
    {
      "name": "qwen3:14b",
      "model": "qwen3:14b",
      "size": 12917222528,
      "size_vram": 12917222528,
      "context_length": 32768,
      "details": {
        "parameter_size": "14.8B",
        "quantization_level": "Q4_K_M"
      }
    }
  ]
}
```

✅ **Статус:** qwen2.5:7b отсутствует в списке. Осталась только qwen3:14b (12.9GB VRAM).

---

## Шаг 3: Загрузка phi4-mini

```bash
curl http://192.168.1.145:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model": "phi4-mini", "keep_alive": -1, "prompt": "hi", "stream": false}'
```

**Результат:**
```json
{
  "model": "phi4-mini",
  "created_at": "2026-03-27T00:53:08.037452863Z",
  "response": "Hello! How can I assist you today?",
  "done": true,
  "done_reason": "stop",
  "load_duration": 49482328720,  // ~49 секунд
  "prompt_eval_duration": 26228693990,  // ~26 секунд
  "eval_duration": 122513635  // ~0.12 секунд
}
```

✅ **Статус:** Модель успешно загружена и ответила на тестовый промпт.

**Время загрузки:** ~49 секунд (load_duration)

---

## Шаг 4: Проверка VRAM (после загрузки phi4-mini)

```bash
curl http://192.168.1.145:11434/api/ps
```

**Результат:**
```json
{
  "models": [
    {
      "name": "phi4-mini:latest",
      "model": "phi4-mini:latest",
      "size": 2886276096,
      "size_vram": 2886276096,
      "context_length": 4096,
      "details": {
        "parameter_size": "3.8B",
        "quantization_level": "Q4_K_M"
      }
    },
    {
      "name": "qwen3:14b",
      "model": "qwen3:14b",
      "size": 12917222528,
      "size_vram": 12917222528,
      "context_length": 32768,
      "details": {
        "parameter_size": "14.8B",
        "quantization_level": "Q4_K_M"
      }
    }
  ]
}
```

---

## 📊 Итоговые данные

### Phi4-Mini характеристики

| Параметр | Значение |
|----------|----------|
| **Модель** | phi4-mini:latest |
| **Размер на диске** | 2.89GB (2886276096 bytes) |
| **VRAM usage** | 2.89GB (2886276096 bytes) |
| **Параметры** | 3.8B |
| **Квантизация** | Q4_K_M |
| **Контекст** | 4096 токенов |
| **Семья** | phi3 |
| **keep_alive** | -1 (навсегда) |

### VRAM распределение

| Модель | VRAM | % от 20GB |
|--------|------|-----------|
| **qwen3:14b** | 12.92GB | 64.6% |
| **phi4-mini** | 2.89GB | 14.5% |
| **Итого** | 15.81GB | 79.1% |
| **Свободно** | 4.19GB | 20.9% |

### Сравнение с qwen2.5:7b

| Модель | VRAM | Экономия |
|--------|------|----------|
| **qwen2.5:7b** | 6.91GB | - |
| **phi4-mini** | 2.89GB | **4.02GB (58% экономия)** |

---

## 🎯 Выводы

1. ✅ **Замена успешна:** phi4-mini загружена вместо qwen2.5:7b
2. ✅ **Экономия VRAM:** 4.02GB (58% меньше чем qwen2.5:7b)
3. ✅ **Время загрузки:** ~49 секунд (приёмлемое)
4. ✅ **Свободно VRAM:** 4.19GB (20.9%) — можно загрузить ещё модели

### GPU distribution

**Важно:** Нужно проверить на каком GPU загружена phi4-mini.

```bash
nvidia-smi | grep -A 10 "phi4\|qwen"
```

**Ожидаемое:** phi4-mini должна загрузиться на тот же GPU что и qwen3:14b (GPU-6d81ac73-73eb-80e9-2930-460dc9d69409 или GPU-a53eeed2-c3af-d78c-f6b7-4999b50c41eb).

---

## 📝 Следующие шаги

1. **Проверить GPU assignment:**
   ```bash
   nvidia-smi
   ```

2. **Обновить warmup.sh:**
   - Заменить qwen2.5:7b на phi4-mini
   - Обновить контекст (4096 вместо 28672)
   - Обновить VRAM расчёты

3. **Тестирование:**
   - Протестировать phi4-mini для deployment задач
   - Сравнить качество с qwen2.5:7b

---

**Записано:** 2026-03-27 00:53 UTC  
**Статус:** ✅ Замена выполнена успешно
