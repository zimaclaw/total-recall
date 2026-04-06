"""
session_store.py — запись и чтение сессии из PostgreSQL.
Вызывается из handler.js через execFileSync.
"""

import json
import argparse
import subprocess
import uuid
import re
import httpx
import psycopg2
import psycopg2.extras
from pathlib import Path
from config import settings

# ─── Конфиг ────────────────────────────────────────────────────────────────

PG_DSN    = settings.pg_dsn
BGE_URL   = str(settings.embed_url).rsplit("/api", 1)[0]
BGE_MODEL = settings.embed_model
SUMMARY_URL = settings.summary_model_url
SUMMARY_MODEL = settings.summary_model
SUMMARY_PROMPT = settings.summary_prompt
SUMMARY_THRESHOLD = settings.summary_threshold

# ─── Helpers ───────────────────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    resp = httpx.post(
        f"{BGE_URL}/api/embeddings",
        json={"model": BGE_MODEL, "prompt": text},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]

def get_conn():
    return psycopg2.connect(PG_DSN)

def call_llm(prompt: str) -> str:
    """Вызов LLM через ollama API для создания summary."""
    resp = httpx.post(
        f"{SUMMARY_URL}/api/chat",
        json={
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


# ─── Archive helpers ───────────────────────────────────────────────────────

LOG_FILE = "/tmp/total-recall.log"

def log_archive(msg: str):
    """Логирование с префиксом [archive]."""
    from datetime import datetime
    timestamp = datetime.now().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] [archive] {msg}\n")


def extract_text_from_message(msg: dict) -> str:
    """
    Извлечь чистый текст из сообщения.
    Фильтрует thinking, injected контекст (=== ... ===).
    """
    content = msg.get('content', '')
    
    # Если content — массив (стандартный формат)
    if isinstance(content, list):
        text_parts = [
            c.get('text', '') for c in content 
            if isinstance(c, dict) and c.get('type') == 'text'
        ]
        clean_content = '\n'.join(text_parts)
    elif isinstance(content, str):
        clean_content = content
    else:
        return ''
    
    # Удаляем injected блоки (=== MEMORY CONTEXT === ... === END MEMORY CONTEXT ===)
    # Regex: === .*? ===[\s\S]*?=== END .*? ===
    clean_content = re.sub(
        r'=== .+? ===[\s\S]*?=== END .+? ===\n?',
        '',
        clean_content
    ).strip()
    
    return clean_content


def archive_session_file(session_id: str, filepath: str) -> dict:
    """
    Архивирует один .jsonl файл в PostgreSQL.
    Возвращает: {"archived": int, "skipped": int, "errors": int}
    """
    log_archive(f"Starting archive: {session_id} from {filepath}")
    
    stats = {"archived": 0, "skipped": 0, "errors": 0}
    
    try:
        with open(filepath, 'r', encoding='utf8') as f:
            lines = f.readlines()
    except Exception as e:
        log_archive(f"ERROR reading {filepath}: {e}")
        return stats
    
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            log_archive(f"ERROR line {line_num}: invalid JSON - {e}")
            stats["errors"] += 1
            continue
        
        # Пропускаем не-сообщения
        if entry.get('type') != 'message':
            continue
        
        msg = entry.get('message', {})
        
        # Пропускаем injected контекст от OpenClaw
        if entry.get('provider') == 'openclaw':
            stats["skipped"] += 1
            continue
        if entry.get('api') == 'openai-responses':
            stats["skipped"] += 1
            continue
        
        # Пропускаем не-user/assistant роли
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            stats["skipped"] += 1
            continue
        
        # Извлекаем чистый текст
        content = extract_text_from_message(msg)
        
        # Пропускаем пустые сообщения
        if not content:
            stats["skipped"] += 1
            continue
        
        # Пишем в PostgreSQL через cmd_message_write
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sessions SET last_at = now() WHERE id = %s",
                        (session_id,)
                    )
                    cur.execute("""
                        INSERT INTO session_messages (session_id, role, content, ts)
                        VALUES (%s, %s, %s, now())
                        RETURNING id
                    """, (session_id, role, content))
                    message_id = cur.fetchone()[0]
                    
                    if role == "user":
                        try:
                            vector = embed(content)
                            cur.execute("""
                                INSERT INTO session_vectors (message_id, session_id, embedding, ts)
                                VALUES (%s, %s, %s::vector, now())
                            """, (message_id, session_id, vector))
                        except Exception as e:
                            log_archive(f"WARNING: embed failed for msg {message_id}: {e}")
                    
                conn.commit()
                stats["archived"] += 1
                
        except Exception as e:
            log_archive(f"ERROR writing message line {line_num}: {e}")
            stats["errors"] += 1
    
    log_archive(f"Complete: {session_id} → {stats['archived']} archived, {stats['skipped']} skipped, {stats['errors']} errors")
    return stats


def cmd_archive_sessions_from_jsonl():
    """
    Архивирует все .jsonl файлы из ~/.openclaw/agents/main/sessions/ в PostgreSQL.
    Пропускает сессии которые уже есть в базе.
    """
    home = Path.home()
    sessions_dir = home / '.openclaw' / 'agents' / 'main' / 'sessions'
    
    if not sessions_dir.exists():
        log_archive(f"Sessions dir not found: {sessions_dir}")
        print(json.dumps({"ok": False, "error": f"Sessions dir not found: {sessions_dir}"}))
        return
    
    # Найти все .jsonl файлы (без .deleted)
    jsonl_files = []
    for f in sessions_dir.glob('*.jsonl*'):
        # Пропускаем .deleted файлы
        if '.deleted.' in f.name:
            continue
        # Берём активные (.jsonl) и сбросы (.jsonl.reset.*)
        if f.suffix == '.jsonl' or '.reset.' in f.name:
            jsonl_files.append(f)
    
    log_archive(f"Found {len(jsonl_files)} session files to process")
    
    total_archived = 0
    total_skipped = 0
    sessions_skipped = 0
    
    for jsonl_file in jsonl_files:
        # Извлечь sessionId из имени файла
        # Формат: <uuid>.jsonl или <uuid>.jsonl.reset.<timestamp>
        # Пример: 0dd9c9f5-77d1-4fb6-9753-80e1fae9db84.jsonl.reset.2026-03-30T20-19-41.634Z
        filename = jsonl_file.name  # полное имя файла
        
        # Убираем .jsonl и всё что после
        if '.jsonl' in filename:
            filename = filename.split('.jsonl')[0]
        elif filename.endswith('.jsonl'):
            filename = filename[:-6]
        
        # Если есть .reset. — убираем timestamp
        if '.reset.' in filename:
            filename = filename.split('.reset.')[0]
        
        session_id = filename
        
        # Валидация UUID
        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', session_id):
            log_archive(f"Skipping invalid filename: {jsonl_file.name}")
            continue
        
        # Проверить есть ли сессия в базе
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM sessions WHERE id = %s",
                    (session_id,)
                )
                count = cur.fetchone()[0]
                
                if count > 0:
                    log_archive(f"Session {session_id} already in DB, skipping")
                    sessions_skipped += 1
                    continue
        
        # Архивируем сессию
        log_archive(f"Archiving session {session_id} from {jsonl_file.name}")
        
        # Сначала создаём запись о сессии
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sessions (id, started_at, last_at)
                    VALUES (%s, now(), now())
                    ON CONFLICT (id) DO NOTHING
                """, (session_id,))
            conn.commit()
        
        # Архивируем сообщения
        stats = archive_session_file(str(session_id), str(jsonl_file))
        total_archived += stats["archived"]
        total_skipped += stats["skipped"]
    
    log_archive(f"Archive complete: {total_archived} messages archived, {total_skipped} skipped, {sessions_skipped} sessions already in DB")
    
    print(json.dumps({
        "ok": True,
        "files_processed": len(jsonl_files),
        "messages_archived": total_archived,
        "messages_skipped": total_skipped,
        "sessions_skipped": sessions_skipped
    }))


# ─── Команды ───────────────────────────────────────────────────────────────

def cmd_session_start(session_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sessions (id, started_at, last_at)
                VALUES (%s, now(), now())
                ON CONFLICT (id) DO UPDATE SET last_at = now()
            """, (session_id,))
        conn.commit()
    print(json.dumps({"ok": True, "session_id": session_id}))


def cmd_message_write(session_id: str, role: str, content: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_at = now() WHERE id = %s",
                (session_id,)
            )
            cur.execute("""
                INSERT INTO session_messages (session_id, role, content, ts)
                VALUES (%s, %s, %s, now())
                RETURNING id
            """, (session_id, role, content))
            message_id = cur.fetchone()[0]

            if role == "user":
                vector = embed(content)
                cur.execute("""
                    INSERT INTO session_vectors (message_id, session_id, embedding, ts)
                    VALUES (%s, %s, %s::vector, now())
                """, (message_id, session_id, vector))

        conn.commit()
    print(json.dumps({"ok": True, "message_id": message_id}))


def cmd_pair_write(session_id: str, user_content: str, assistant_content: str):
    """
    Запись пары user+assistant с общим pair_id.
    Триггерит summary_build в фоне если достигнут порог.
    """
    pair_id = str(uuid.uuid4())
    summary_triggered = False
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Обновляем last_at
            cur.execute(
                "UPDATE sessions SET last_at = now() WHERE id = %s",
                (session_id,)
            )
            
            # INSERT user сообщение с pair_id
            cur.execute("""
                INSERT INTO session_messages (session_id, role, content, pair_id, ts)
                VALUES (%s, 'user', %s, %s, now())
                RETURNING id
            """, (session_id, user_content, pair_id))
            user_message_id = cur.fetchone()[0]
            
            # Embed user content
            vector = embed(user_content)
            cur.execute("""
                INSERT INTO session_vectors (message_id, session_id, embedding, ts)
                VALUES (%s, %s, %s::vector, now())
            """, (user_message_id, session_id, vector))
            
            # INSERT assistant сообщение с pair_id
            cur.execute("""
                INSERT INTO session_messages (session_id, role, content, pair_id, ts)
                VALUES (%s, 'assistant', %s, %s, now())
                RETURNING id
            """, (session_id, assistant_content, pair_id))
            
            # Считаем COUNT пар в сессии
            cur.execute("""
                SELECT COUNT(DISTINCT pair_id) as pair_count
                FROM session_messages
                WHERE session_id = %s
            """, (session_id,))
            pair_count = cur.fetchone()[0]
            
            # Проверяем триггер: COUNT >= threshold И (COUNT - 10) % 10 == 0
            if pair_count >= SUMMARY_THRESHOLD and (pair_count - 10) % 10 == 0:
                summary_triggered = True
                conn.commit()
                
                # Запускаем summary_build в фоне (не блокируем)
                subprocess.Popen([
                    "python", __file__,
                    "summary_build",
                    "--session-id", session_id,
                ])
                print(json.dumps({
                    "pair_id": pair_id,
                    "summary_triggered": True,
                    "pair_count": pair_count
                }))
                return
        
        conn.commit()
    
    print(json.dumps({
        "pair_id": pair_id,
        "summary_triggered": False,
        "pair_count": pair_count
    }))


def cmd_skeleton(session_id: str, max_tokens: int = 2000):
    limit_chars = max_tokens * 4
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT content, ts
                FROM session_messages
                WHERE session_id = %s AND role = 'user'
                ORDER BY ts ASC
            """, (session_id,))
            rows = cur.fetchall()

    if not rows:
        print(json.dumps({"skeleton": ""}))
        return

    lines = [f"[{r['ts'].strftime('%H:%M')}] {r['content']}" for r in rows]
    text = "\n".join(lines)

    if len(text) > limit_chars:
        text = "...(обрезано)\n" + text[-limit_chars:]

    print(json.dumps({"skeleton": text}))


def cmd_focus(session_id: str, query: str = None, top_k: int = 5, min_score: float = 0.4):
    """
    Обновлённая команда focus — возвращает sliding context window:
    [Summary] + [Пара-связка] + [Хвост (10 пар)]
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Получаем последние 10 пар (20 сообщений)
            cur.execute("""
                SELECT role, content, ts, pair_id
                FROM session_messages
                WHERE session_id = %s
                ORDER BY ts DESC
                LIMIT 20
            """, (session_id,))
            tail_messages = list(cur.fetchall())
            
            # Группируем в пары
            tail_pairs = []
            current_pair = {}
            for msg in tail_messages:
                if msg['pair_id'] and msg['pair_id'] not in [p.get('pair_id') for p in tail_pairs]:
                    if current_pair:
                        tail_pairs.append(current_pair)
                    current_pair = {
                        'pair_id': msg['pair_id'],
                        msg['role']: msg
                    }
                elif msg['pair_id']:
                    current_pair[msg['role']] = msg
            if current_pair:
                tail_pairs.append(current_pair)
            tail_pairs.reverse()
            
            # Если пар меньше 10 — просто возвращаем их
            if len(tail_pairs) < 10:
                # Форматируем хвост
                tail_lines = []
                for pair in tail_pairs:
                    if 'user' in pair and 'assistant' in pair:
                        tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                        tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                    elif 'user' in pair:
                        tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                    elif 'assistant' in pair:
                        tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                
                focus_text = "\n".join(tail_lines) if tail_lines else ""
                print(json.dumps({"focus": focus_text}))
                return
            
            # Получаем summary
            cur.execute("""
                SELECT summary, pairs_count, last_pair_id, mode
                FROM session_summary
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (session_id,))
            summary_row = cur.fetchone()
            
            if not summary_row:
                # Нет summary — просто возвращаем хвост
                tail_lines = []
                for pair in tail_pairs[:10]:
                    if 'user' in pair and 'assistant' in pair:
                        tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                        tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                    elif 'user' in pair:
                        tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                    elif 'assistant' in pair:
                        tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                
                focus_text = "\n".join(tail_lines) if tail_lines else ""
                print(json.dumps({"focus": focus_text}))
                return
            
            # Есть summary — ищем пару-связку (последнюю вошедшую в summary)
            anchor_pair = None
            if summary_row['last_pair_id']:
                cur.execute("""
                    SELECT role, content, ts
                    FROM session_messages
                    WHERE session_id = %s AND pair_id = %s
                    ORDER BY ts ASC
                """, (session_id, summary_row['last_pair_id']))
                anchor_msgs = cur.fetchall()
                if anchor_msgs:
                    anchor_pair = {
                        'pair_id': summary_row['last_pair_id'],
                        **{msg['role']: msg for msg in anchor_msgs}
                    }
            
            # Форматируем результат
            result_lines = []
            
            # Summary
            result_lines.append("=== SUMMARY ===")
            result_lines.append(summary_row['summary'])
            result_lines.append("")
            
            # Пара-связка
            if anchor_pair:
                result_lines.append("=== ПРОДОЛЖЕНИЕ ===")
                if 'user' in anchor_pair:
                    result_lines.append(f"[USER {anchor_pair['user']['ts'].strftime('%H:%M')}] {anchor_pair['user']['content']}")
                if 'assistant' in anchor_pair:
                    result_lines.append(f"[ASSISTANT {anchor_pair['assistant']['ts'].strftime('%H:%M')}] {anchor_pair['assistant']['content']}")
                result_lines.append("")
            
            # Хвост (последние 10 пар)
            result_lines.append("=== ХВОСТ ===")
            for pair in tail_pairs[:10]:
                if 'user' in pair and 'assistant' in pair:
                    result_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                    result_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                elif 'user' in pair:
                    result_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                elif 'assistant' in pair:
                    result_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
            
            focus_text = "\n".join(result_lines)
            print(json.dumps({"focus": focus_text}))


def cmd_summary_build(session_id: str, mode: str = None):
    """
    Создаёт summary сессии через LLM.
    mode: 'full' — summary всех пар кроме последних 10
           'incremental' — обновление существующего summary
    """
    if mode is None:
        mode = settings.summary_mode
    
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if mode == 'full':
                # Получаем все пары кроме последних 10
                cur.execute("""
                    SELECT DISTINCT ON (pair_id) pair_id
                    FROM session_messages
                    WHERE session_id = %s AND pair_id IS NOT NULL
                    ORDER BY pair_id, ts ASC
                    OFFSET 10
                """, (session_id,))
                pairs_to_summarize = cur.fetchall()
                
                if not pairs_to_summarize:
                    print(json.dumps({"summary": "", "pairs_count": 0}))
                    return
                
                # Получаем содержимое пар
                pair_ids = [p['pair_id'] for p in pairs_to_summarize]
                placeholders = ",".join(["%s"] * len(pair_ids))
                cur.execute(f"""
                    SELECT role, content, ts
                    FROM session_messages
                    WHERE session_id = %s AND pair_id IN ({placeholders})
                    ORDER BY ts ASC
                """, (session_id, *pair_ids))
                messages = cur.fetchall()
                
                # Формируем промпт
                pairs_text = []
                for msg in messages:
                    pairs_text.append(f"[{msg['role'].upper()}] {msg['content']}")
                pairs_text = "\n".join(pairs_text)
                
                prompt = SUMMARY_PROMPT.format(pairs=pairs_text)
                
                # Вызываем LLM
                summary = call_llm(prompt)
                
                # Сохраняем
                cur.execute("""
                    INSERT INTO session_summary (session_id, summary, pairs_count, mode, created_at)
                    VALUES (%s, %s, %s, %s, now())
                """, (session_id, summary, len(pairs_to_summarize), mode))
                
                conn.commit()
                print(json.dumps({"summary": summary, "pairs_count": len(pairs_to_summarize)}))
                
            elif mode == 'incremental':
                # Получаем последний summary
                cur.execute("""
                    SELECT id, summary, pairs_count, last_pair_id
                    FROM session_summary
                    WHERE session_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (session_id,))
                last_summary = cur.fetchone()
                
                if not last_summary:
                    # Нет старого summary — делаем full
                    mode = 'full'
                    return cmd_summary_build(session_id, mode)
                
                # Получаем новые пары после last_pair_id
                if last_summary['last_pair_id']:
                    cur.execute("""
                        SELECT DISTINCT ON (pair_id) pair_id, MAX(ts) as pair_ts
                        FROM session_messages
                        WHERE session_id = %s AND pair_id IS NOT NULL AND pair_id != %s
                        GROUP BY pair_id
                        HAVING MAX(ts) > (
                            SELECT ts FROM session_messages 
                            WHERE pair_id = %s LIMIT 1
                        )
                        ORDER BY pair_id, pair_ts ASC
                    """, (session_id, last_summary['last_pair_id'], last_summary['last_pair_id']))
                    new_pairs = cur.fetchall()
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (pair_id) pair_id, MAX(ts) as pair_ts
                        FROM session_messages
                        WHERE session_id = %s AND pair_id IS NOT NULL
                        GROUP BY pair_id
                        ORDER BY pair_id, pair_ts ASC
                    """, (session_id,))
                    new_pairs = cur.fetchall()
                
                if not new_pairs:
                    print(json.dumps({"summary": last_summary['summary'], "pairs_count": last_summary['pairs_count']}))
                    return
                
                # Получаем содержимое новых пар
                pair_ids = [p['pair_id'] for p in new_pairs]
                placeholders = ",".join(["%s"] * len(pair_ids))
                cur.execute(f"""
                    SELECT role, content, ts
                    FROM session_messages
                    WHERE session_id = %s AND pair_id IN ({placeholders})
                    ORDER BY ts ASC
                """, (session_id, *pair_ids))
                new_messages = cur.fetchall()
                
                # Формируем промпт
                new_pairs_text = []
                for msg in new_messages:
                    new_pairs_text.append(f"[{msg['role'].upper()}] {msg['content']}")
                new_pairs_text = "\n".join(new_pairs_text)
                
                prompt = f"Существующее summary:\n{last_summary['summary']}\n\nНовые части диалога:\n{new_pairs_text}\n\nОбнови summary сохранив все важные факты."
                
                # Вызываем LLM
                updated_summary = call_llm(prompt)
                
                # Сохраняем новый summary
                last_pair_id = new_pairs[-1]['pair_id'] if new_pairs else None
                cur.execute("""
                    INSERT INTO session_summary (session_id, summary, pairs_count, last_pair_id, mode, created_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                """, (session_id, updated_summary, last_summary['pairs_count'] + len(new_pairs), last_pair_id, mode))
                
                conn.commit()
                print(json.dumps({"summary": updated_summary, "pairs_count": last_summary['pairs_count'] + len(new_pairs)}))


def cmd_summary_get(session_id: str):
    """Получает последний summary сессии."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT summary, pairs_count, mode
                FROM session_summary
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (session_id,))
            row = cur.fetchone()
            
            if not row:
                print(json.dumps({"summary": None}))
                return
            
            print(json.dumps({
                "summary": row['summary'],
                "pairs_count": row['pairs_count'],
                "mode": row['mode']
            }))


def cmd_focus_simple(session_id: str):
    """
    Упрощённая версия focus — только последние 10 пар без семантического поиска.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Получаем последние 10 пар (20 сообщений)
            cur.execute("""
                SELECT role, content, ts, pair_id
                FROM session_messages
                WHERE session_id = %s
                ORDER BY ts DESC
                LIMIT 20
            """, (session_id,))
            tail_messages = list(cur.fetchall())
            
            # Группируем в пары (обратный порядок — от новых к старым)
            tail_pairs = []
            seen_pair_ids = set()
            for msg in tail_messages:
                if msg['pair_id'] and msg['pair_id'] not in seen_pair_ids:
                    if msg['pair_id'] not in seen_pair_ids:
                        seen_pair_ids.add(msg['pair_id'])
                        # Находим оба сообщения пары
                        pair_msgs = [m for m in tail_messages if m['pair_id'] == msg['pair_id']]
                        tail_pairs.append({
                            'pair_id': msg['pair_id'],
                            **{m['role']: m for m in pair_msgs}
                        })
            # Разворачиваем чтобы старые были первыми
            tail_pairs.reverse()
            
            # Форматируем
            tail_lines = []
            for pair in tail_pairs:
                if 'user' in pair and 'assistant' in pair:
                    tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                    tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
                elif 'user' in pair:
                    tail_lines.append(f"[USER {pair['user']['ts'].strftime('%H:%M')}] {pair['user']['content']}")
                elif 'assistant' in pair:
                    tail_lines.append(f"[ASSISTANT {pair['assistant']['ts'].strftime('%H:%M')}] {pair['assistant']['content']}")
            
            focus_text = "\n".join(tail_lines) if tail_lines else ""
            print(json.dumps({"focus": focus_text}))


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("session_start")
    p.add_argument("--session-id", required=True)

    p = sub.add_parser("message_write")
    p.add_argument("--session-id", required=True)
    p.add_argument("--role", required=True, choices=["user", "assistant"])
    p.add_argument("--content", required=True)

    p = sub.add_parser("pair_write")
    p.add_argument("--session-id", required=True)
    p.add_argument("--user-content", required=True)
    p.add_argument("--assistant-content", required=True)

    p = sub.add_parser("skeleton")
    p.add_argument("--session-id", required=True)
    p.add_argument("--max-tokens", type=int, default=2000)

    p = sub.add_parser("focus")
    p.add_argument("--session-id", required=True)
    p.add_argument("--query", required=False)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-score", type=float, default=0.4)

    p = sub.add_parser("focus_simple")
    p.add_argument("--session-id", required=True)

    p = sub.add_parser("summary_build")
    p.add_argument("--session-id", required=True)
    p.add_argument("--mode", choices=["full", "incremental"], default=None)

    p = sub.add_parser("summary_get")
    p.add_argument("--session-id", required=True)

    p = sub.add_parser("archive_sessions_from_jsonl")

    args = parser.parse_args()

    if args.cmd == "session_start":
        cmd_session_start(args.session_id)
    elif args.cmd == "message_write":
        cmd_message_write(args.session_id, args.role, args.content)
    elif args.cmd == "pair_write":
        cmd_pair_write(args.session_id, args.user_content, args.assistant_content)
    elif args.cmd == "skeleton":
        cmd_skeleton(args.session_id, args.max_tokens)
    elif args.cmd == "focus":
        cmd_focus(args.session_id, args.query, args.top_k, args.min_score)
    elif args.cmd == "focus_simple":
        cmd_focus_simple(args.session_id)
    elif args.cmd == "summary_build":
        cmd_summary_build(args.session_id, args.mode)
    elif args.cmd == "summary_get":
        cmd_summary_get(args.session_id)
    elif args.cmd == "archive_sessions_from_jsonl":
        cmd_archive_sessions_from_jsonl()


if __name__ == "__main__":
    main()
