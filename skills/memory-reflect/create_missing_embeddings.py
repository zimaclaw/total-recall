#!/usr/bin/env python3
"""Создать embedding для Conclusions без embedding."""

import os
import sys
import requests
from datetime import datetime

# Добавить путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from neo4j import GraphDatabase
from config import Settings

def main():
    settings = Settings()
    
    # Подключение к Neo4j
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password)
    )
    
    # Ollama embeddings
    embed_url = os.getenv('OLLAMA_EMBEDDINGS_URL', 'http://192.168.1.145:11435/api/embeddings')
    model = os.getenv('OLLAMA_EMBEDDINGS_MODEL', 'bge-m3')
    
    print(f"Создание embedding для Conclusions без embedding...")
    print(f"Ollama: {embed_url}, модель: {model}")
    print()
    
    with driver.session() as session:
        # Найти Conclusions без embedding
        result = session.run("""
            MATCH (c:Conclusion)
            WHERE c.embedding IS NULL
            RETURN c.conclusion_id, c.insight, c.ts_created
            ORDER BY c.ts_created DESC
        """)
        
        conclusions = [dict(row) for row in result]
        print(f"Найдено {len(conclusions)} Conclusions без embedding")
        print()
        
        created = 0
        failed = 0
        
        for row in conclusions:
            conclusion_id = row['c.conclusion_id']
            insight = row['c.insight'] or ""
            ts = datetime.fromtimestamp(row['c.ts_created'])
            
            print(f"[{ts}] {conclusion_id[:8]}... | {insight[:60]}...")
            
            # Создать embedding
            try:
                response = requests.post(
                    embed_url,
                    json={"model": model, "prompt": insight},
                    timeout=30
                )
                
                if response.status_code == 200:
                    embedding = response.json()["embedding"]
                    session.run("""
                        MATCH (c:Conclusion {conclusion_id: $id})
                        SET c.embedding = $embedding
                    """, id=conclusion_id, embedding=embedding)
                    print(f"  ✅ Created ({len(embedding)} dims)")
                    created += 1
                else:
                    print(f"  ❌ HTTP {response.status_code}")
                    failed += 1
            except Exception as e:
                print(f"  ❌ Error: {e}")
                failed += 1
        
        print()
        print(f"Результат: {created} создано, {failed} ошибок")
        
        # Проверить результат
        result = session.run("""
            MATCH (c:Conclusion)
            RETURN 
                count(c) as total,
                count(c.embedding) as with_embedding,
                count(c.embedding IS NULL) as without_embedding
        """)
        
        for row in result:
            d = dict(row)
            print(f"Всего: {d['total']}, с embedding: {d['with_embedding']}, без: {d['without_embedding']}")
    
    driver.close()

if __name__ == "__main__":
    main()
