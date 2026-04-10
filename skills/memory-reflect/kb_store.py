"""
kb_store.py — CLI для работы с Knowledge Base.
Вызывается из handler.js через execFileSync.
"""

import json
import argparse
import httpx
import psycopg2
import sys
from pathlib import Path

# Добавляем config в путь
sys.path.insert(0, str(Path(__file__).parent.parent / 'projects' / 'total-recall' / 'config'))
from config import settings
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

# ─── Конфиг ───────────────────────────────────────────────────────────────────

PG_DSN    = settings.pg_dsn
BGE_URL   = str(settings.embed_url).rsplit("/api", 1)[0]
BGE_MODEL = settings.embed_model
import os as _os
_all_proxy = _os.environ.pop('ALL_PROXY', None)
_all_proxy_lower = _os.environ.pop('all_proxy', None)
QDRANT_CLIENT = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port, check_compatibility=False)
if _all_proxy: _os.environ['ALL_PROXY'] = _all_proxy
if _all_proxy_lower: _os.environ['all_proxy'] = _all_proxy_lower

# ─── Helpers ───────────────────────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    """Создать embedding через bge-m3."""
    with httpx.Client(transport=httpx.HTTPTransport(), timeout=10.0) as client:
        resp = client.post(
            f"{BGE_URL}/api/embeddings",
            json={"model": BGE_MODEL, "prompt": text},
        )
    resp.raise_for_status()
    return resp.json()["embedding"]

def get_conn():
    """Подключение к PostgreSQL."""
    return psycopg2.connect(PG_DSN)

# ─── Команды ───────────────────────────────────────────────────────────────────

def cmd_kb_save(title: str, summary: str, content: str, source_url: str, source_tool: str, category: str):
    """Сохранить запись в kb_hot."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kb_hot (title, summary, content, source_url, source_tool, category)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (title, summary, content, source_url, source_tool, category))
            kb_id = cur.fetchone()[0]
        conn.commit()
    
    # Embed summary и upsert в Qdrant
    vector = embed(summary)
    QDRANT_CLIENT.upsert(
        collection_name="knowledge_base",
        points=[PointStruct(
            id=str(kb_id),
            vector=vector,
            payload={
                "kb_id": str(kb_id),
                "title": title,
                "summary": summary,
                "category": category,
                "is_stale": False,
            }
        )]
    )
    
    print(json.dumps({"id": str(kb_id)}))


def cmd_kb_promote(kb_id: str):
    """Переместить из kb_hot в kb_cold."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # SELECT из kb_hot
            cur.execute("SELECT * FROM kb_hot WHERE id = %s", (kb_id,))
            row = cur.fetchone()
            if not row:
                print(json.dumps({"ok": False, "error": "not found"}))
                return
            
            # INSERT в kb_cold
            cur.execute("""
                INSERT INTO kb_cold (id, source_url, source_tool, title, summary, content, category, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]))
            
            # DELETE из kb_hot
            cur.execute("DELETE FROM kb_hot WHERE id = %s", (kb_id,))
        conn.commit()
    
    # Обновить payload в Qdrant
    QDRANT_CLIENT.update_payload(
        collection_name="knowledge_base",
        point_id=str(kb_id),
        update_payload={"is_stale": False}
    )
    
    print(json.dumps({"ok": True}))


def cmd_kb_fetch(kb_id: str):
    """Получить контент по ID."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Пробуем kb_hot
            cur.execute("SELECT title, content FROM kb_hot WHERE id = %s", (kb_id,))
            row = cur.fetchone()
            
            # Если нет — kb_cold
            if not row:
                cur.execute("SELECT title, content FROM kb_cold WHERE id = %s", (kb_id,))
                row = cur.fetchone()
                
                if row:
                    # UPDATE last_accessed_at, access_count++
                    cur.execute("""
                        UPDATE kb_cold 
                        SET last_accessed_at = now(), access_count = access_count + 1 
                        WHERE id = %s
                    """, (kb_id,))
                    conn.commit()
            
            if not row:
                print(json.dumps({"error": "not found"}))
                return
            
            print(json.dumps({"id": kb_id, "title": row[0], "content": row[1]}))


def cmd_kb_search(query: str, category: str = None, limit: int = 5):
    """Векторный поиск по KB."""
    from qdrant_client.http import models
    
    query_vec = embed(query)
    
    # Поиск в Qdrant через query_points
    results = QDRANT_CLIENT.query_points(
        collection_name="knowledge_base",
        query=query_vec,
        limit=limit,
        query_filter=None if category is None else models.Filter(
            must=[models.FieldCondition(key="category", match=models.MatchValue(value=category))]
        )
    )
    
    # Форматируем результат
    formatted = []
    for hit in results.points:
        payload = hit.payload
        formatted.append({
            "id": payload["kb_id"],
            "title": payload["title"],
            "summary": payload["summary"],
            "is_stale": payload.get("is_stale", False),
            "score": hit.score
        })
    
    print(json.dumps({"results": formatted}))


def cmd_kb_cleanup():
    """Удалить истёкшие записи из kb_hot."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM kb_hot 
                WHERE expires_at < now() AND promoted = false
            """)
            deleted = cur.rowcount
        conn.commit()
    
    print(json.dumps({"deleted": deleted}))


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KB Store CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    # kb_save
    p = sub.add_parser("kb_save", help="Save record to KB")
    p.add_argument("--title", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--source-url", default="")
    p.add_argument("--source-tool", default="")
    p.add_argument("--category", default="search")
    
    # kb_promote
    p = sub.add_parser("kb_promote", help="Promote from hot to cold")
    p.add_argument("--id", required=True)
    
    # kb_fetch
    p = sub.add_parser("kb_fetch", help="Fetch record by ID")
    p.add_argument("--id", required=True)
    
    # kb_search
    p = sub.add_parser("kb_search", help="Search KB")
    p.add_argument("--query", required=True)
    p.add_argument("--category", default=None)
    p.add_argument("--limit", type=int, default=5)
    
    # kb_cleanup
    p = sub.add_parser("kb_cleanup", help="Cleanup expired records")
    
    args = parser.parse_args()
    
    if args.cmd == "kb_save":
        cmd_kb_save(args.title, args.summary, args.content, args.source_url, args.source_tool, args.category)
    elif args.cmd == "kb_promote":
        cmd_kb_promote(args.id)
    elif args.cmd == "kb_fetch":
        cmd_kb_fetch(args.id)
    elif args.cmd == "kb_search":
        cmd_kb_search(args.query, args.category, args.limit)
    elif args.cmd == "kb_cleanup":
        cmd_kb_cleanup()


if __name__ == "__main__":
    main()
