# total-recall

Memory and self-learning system for Friday — personal AI assistant on local LLMs.

## What it does

- Captures agent experience after every task (5-field dump)
- Stores facts + graph in Neo4j with Bayesian confidence updates
- Semantic search via bge-m3 + Qdrant for flashback
- Separates **knowledge** (confidence) from **behavior** (mastery)

## Stack

- Neo4j — knowledge graph
- Qdrant collection `reflections` — semantic search
- bge-m3 — embeddings
- Poetry — dependency management

## Quick start
```bash
cd skills/memory-reflect
poetry install
python memory-reflect.py --init-schema
python memory-reflect.py --flashback --category infra
```

## Docs

- [Architecture](docs/architecture.md)
- [Memory schema](docs/memory-schema.md)
- [Dump format](docs/dump-format.md)
