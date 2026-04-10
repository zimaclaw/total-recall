
## Временный debug код — удалить 2026-04-13

`~/.openclaw/skills/memory-reflect/kb_store.py` — начало файла:
```python
import os as _dbg_os
with open('/tmp/proxy_debug.log', 'w') as _f:
    ...
```
Добавлен для диагностики прокси. Удалить после стабильной работы KB.
