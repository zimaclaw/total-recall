"""
session_store.py — запись и чтение сессии из PostgreSQL.
Вызывается из handler.js через execFileSync.
"""

import json
import argparse
import httpx
import psycopg2
import psycopg2.extras
from config import settings

# ─── Конфиг ────────────────────────────────────────────────────────────────

PG_DSN    = settings.pg_dsn
BGE_URL   = str(settings.embed_url).rsplit("/api", 1)[0]
BGE_MODEL = settings.embed_model

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


def cmd_focus(session_id: str, query: str, top_k: int = 5, min_score: float = 0.4):
    query_vec = embed(query)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT sv.message_id,
                       1 - (sv.embedding <=> %s::vector) AS score,
                       sm.content,
                       sm.ts
                FROM session_vectors sv
                JOIN session_messages sm ON sm.id = sv.message_id
                WHERE sv.session_id = %s
                  AND 1 - (sv.embedding <=> %s::vector) >= %s
                ORDER BY score DESC
                LIMIT %s
            """, (query_vec, session_id, query_vec, min_score, top_k))
            hits = cur.fetchall()

            if not hits:
                print(json.dumps({"focus": ""}))
                return

            focus_blocks = []
            seen_ids = set()

            for hit in hits:
                msg_id = hit["message_id"]
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                cur.execute("""
                    SELECT role, content, ts
                    FROM session_messages
                    WHERE session_id = %s AND id >= %s
                    ORDER BY id ASC
                    LIMIT 2
                """, (session_id, msg_id))
                pair = cur.fetchall()

                block = "\n".join(
                    f"[{r['role'].upper()} {r['ts'].strftime('%H:%M')}] {r['content']}"
                    for r in pair
                )
                focus_blocks.append(block)

    print(json.dumps({"focus": "\n---\n".join(focus_blocks)}))


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

    p = sub.add_parser("skeleton")
    p.add_argument("--session-id", required=True)
    p.add_argument("--max-tokens", type=int, default=2000)

    p = sub.add_parser("focus")
    p.add_argument("--session-id", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-score", type=float, default=0.4)

    args = parser.parse_args()

    if args.cmd == "session_start":
        cmd_session_start(args.session_id)
    elif args.cmd == "message_write":
        cmd_message_write(args.session_id, args.role, args.content)
    elif args.cmd == "skeleton":
        cmd_skeleton(args.session_id, args.max_tokens)
    elif args.cmd == "focus":
        cmd_focus(args.session_id, args.query, args.top_k, args.min_score)


if __name__ == "__main__":
    main()
