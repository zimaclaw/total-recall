#!/bin/bash
set -e

OPENCLAW_DIR="/home/ironman/.openclaw"
WORKSPACE="$OPENCLAW_DIR/workspace"
SKILLS_DIR="$WORKSPACE/skills/memory-reflect"

echo "=== total-recall install ==="

# 1. Скопировать скрипт
mkdir -p "$SKILLS_DIR"
cp skills/memory-reflect/memory-reflect.py "$SKILLS_DIR/"
cp skills/memory-reflect/pyproject.toml "$SKILLS_DIR/"
cp skills/memory-reflect/poetry.lock "$SKILLS_DIR/"
echo "✓ Script copied"

# 2. Установить зависимости
cd "$SKILLS_DIR"
poetry install --no-root
cd -
echo "✓ Dependencies installed"

# 3. Создать директорию для дампов
mkdir -p "$WORKSPACE/memory/dumps"
echo "✓ Dumps directory created"

# 4. Инициализировать схему Neo4j
cd "$SKILLS_DIR"
poetry run python memory-reflect.py --init-schema
cd -
echo "✓ Neo4j schema initialized"

# 5. Создать коллекцию Qdrant
curl -sf -X PUT http://192.168.1.145:6333/collections/reflections \
  -H 'Content-Type: application/json' \
  -d '{"vectors": {"size": 1024, "distance": "Cosine"}}' \
  | python3 -m json.tool || echo "Collection may already exist"
echo "✓ Qdrant collection ready"

echo ""
echo "=== Done. Test with: ==="
echo "cd $SKILLS_DIR"
echo "poetry run python memory-reflect.py --flashback --category infra"
