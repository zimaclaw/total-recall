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
    neo4j_uri:      str = "bolt://192.168.1.145:7687"
    neo4j_user:     str = "neo4j"
    neo4j_password: str  # обязательное — нет дефолта

    # ─── PostgreSQL ───────────────────────────────────────────────────────────
    pg_dsn: str  # обязательное — нет дефолта

    # ─── Qdrant ───────────────────────────────────────────────────────────────
    qdrant_host:          str   = "192.168.1.145"
    qdrant_port:          int   = 6333
    similarity_threshold: float = 0.40

    # ─── Embeddings ───────────────────────────────────────────────────────────
    embed_url:   str = "http://192.168.1.145:11435/api/embeddings"
    embed_model: str = "bge-m3:latest"

    # ─── LLM для рефлексии ────────────────────────────────────────────────────
    ollama_url:    str = "http://192.168.1.145:11434/api/chat"
    reflect_model: str = "qwen3:14b"

    # ─── Пути ─────────────────────────────────────────────────────────────────
    dump_dir: Path = Path("/home/ironman/.openclaw/workspace/memory/dumps")
    log_dir:  Path = Path("/home/ironman/.openclaw/workspace/logs")

    # ─── Триггеры демона ──────────────────────────────────────────────────────
    reflect_trigger_count: int = 10
    reflect_trigger_hours: int = 24
    reflect_poll_seconds:  int = 300

    # ─── Байесовские пороги ───────────────────────────────────────────────────
    flashback_threshold:      float = 0.60
    lesson_conf_threshold:    float = 0.75
    lesson_mastery_threshold: float = 0.60
    needs_review_threshold:   float = 0.40

    # ─── Пороги рефлексии ─────────────────────────────────────────────────────
    principle_min_cluster:    int   = 3
    principle_conf_threshold: float = 0.70
    meta_min_cluster:         int   = 2


settings = Settings()

