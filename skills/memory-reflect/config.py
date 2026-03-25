"""
config.py — конфигурация OpenClaw memory stack.
Читает из .env файла (или переменных окружения).
Поля без дефолта — обязательные: упадут при старте если не заданы.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Neo4j ────────────────────────────────────────────────────────────────
    neo4j_uri:      str = "bolt://localhost:7687"
    neo4j_user:     str = "neo4j"
    neo4j_password: str  # обязательное — нет дефолта

    # ─── Qdrant ───────────────────────────────────────────────────────────────
    qdrant_host:       str   = "localhost"
    qdrant_port:       int   = 6333
    qdrant_collection: str   = "reflections"
    similarity_threshold: float = 0.72

    # ─── Embeddings & Reranker ────────────────────────────────────────────────
    embed_url:    str = "http://localhost:11435/api/embed"
    embed_model:  str = "bge-m3:latest"
    rerank_url:   str = "http://localhost:11435/api/rerank"
    rerank_model: str = "xitao/bge-reranker-v2-m3:latest"

    # ─── LLM для рефлексии ────────────────────────────────────────────────────
    ollama_url:    str = "http://localhost:11434/api/chat"
    reflect_model: str = "qwen3:14b"

    # ─── Пути ─────────────────────────────────────────────────────────────────
    dump_dir: Path = Path("/home/ironman/.openclaw/workspace/memory/dumps")
    log_dir:  Path = Path("/home/ironman/.openclaw/workspace/logs")

    # ─── Триггеры демона ──────────────────────────────────────────────────────
    reflect_trigger_count:   int = 10   # новых Conclusion → запустить reflect
    reflect_trigger_hours:   int = 24   # часов без рефлексии → запустить принудительно
    reflect_poll_seconds:    int = 300  # как часто демон проверяет триггеры

    # ─── Байесовские пороги ───────────────────────────────────────────────────
    flashback_threshold:      float = 0.60
    lesson_conf_threshold:    float = 0.75
    lesson_mastery_threshold: float = 0.60
    needs_review_threshold:   float = 0.40

    # ─── Пороги рефлексии (Фаза 2) ───────────────────────────────────────────
    principle_min_cluster:    int   = 3     # минимум Lesson в кластере → Principle
    principle_conf_threshold: float = 0.70  # минимальный avg confidence кластера
    meta_min_cluster:         int   = 2     # минимум Principle → Meta


# Синглтон — импортируется один раз при старте
# Если .env не найден или обязательное поле не задано — ValidationError при импорте
settings = Settings()
