"""
migrate_summary.py — миграция для sliding context window с summary.
Создаёт таблицу session_summary и добавляет pair_id в session_messages.
"""

import sys
from pathlib import Path

# Добавляем config в путь
sys.path.insert(0, str(Path(__file__).parent.parent / 'projects' / 'total-recall' / 'config'))

from config import settings
import psycopg2

def main():
    print(f"📌 Подключение к PostgreSQL: {settings.pg_dsn}")
    
    conn = psycopg2.connect(settings.pg_dsn)
    cur = conn.cursor()
    
    try:
        # Создаём таблицу session_summary
        print("📝 Создание таблицы session_summary...")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS session_summary (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          session_id   TEXT NOT NULL REFERENCES sessions(id),
          summary      TEXT NOT NULL,
          pairs_count  INT NOT NULL,
          last_pair_id UUID,
          mode         TEXT DEFAULT 'incremental',
          created_at   TIMESTAMPTZ DEFAULT now()
        )
        """)
        print("✅ Таблица session_summary создана")
        
        # Добавляем column pair_id в session_messages
        print("📝 Добавление column pair_id в session_messages...")
        cur.execute("""
        ALTER TABLE session_messages 
        ADD COLUMN IF NOT EXISTS pair_id UUID
        """)
        print("✅ Column pair_id добавлен")
        
        conn.commit()
        print("\n✅ OK")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
