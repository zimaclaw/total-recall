#!/usr/bin/env python3
"""
memory-reflect.py v3 — асинхронный скилл OpenClaw
Читает дамп задачи (5 полей), пишет в Neo4j и mem0.

Дамп формат:
{
    "task_id":       "uuid",
    "goal":          "что хотели сделать",
    "outcome":       "success|fail|partial|abandoned",
    "reason":        "почему такой outcome",
    "insight":       "что делать в следующий раз",
    "evidence_type": "empirical|documented|legal|knowledge|inferred|generated",
    "ts":            1705312800,
    "category":      "deploy",          (опционально — иначе выводится)
    "tags":          ["port", "docker"], (опционально)
    "lessons_applied": [                (опционально)
        {"principle": "текст урока", "helped": true}
    ]
}

Использование:
    python3 memory-reflect.py --dump /path/to/task.json
    python3 memory-reflect.py --dump /path/to/task.json --dry-run
    python3 memory-reflect.py --init-schema
    python3 memory-reflect.py --flashback --category deploy
"""

import argparse
import hashlib
import json
import logging
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests as _req_global  # noqa — используется в QdrantSearch через _requests
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError

# ─── Конфиг ───────────────────────────────────────────────────────────────────

NEO4J_URI      = "bolt://192.168.1.145:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "mem0graph"

DUMP_DIR = Path("/home/ironman/.openclaw/workspace/memory/dumps")
LOG_DIR  = Path("/home/ironman/.openclaw/workspace/logs")

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0

# ─── Параметры Байеса ─────────────────────────────────────────────────────────

DECAY_BY_EVIDENCE_TYPE = {
    "legal":     0.001900,  # half-life 1 год
    "knowledge": 0.000317,  # half-life 6 лет
}

DECAY_BY_CATEGORY = {
    "rules":    0.000634,  # 3 года
    "memory":   0.000950,  # 2 года
    "infra":    0.001270,  # 1.5 года
    "deploy":   0.001900,  # 1 год
    "plan":     0.001900,  # 1 год
    "write":    0.001900,  # 1 год
    "user":     0.001900,  # 1 год
    "dev":      0.002534,  # 9 месяцев
    "test":     0.002534,  # 9 месяцев
    "research": 0.003800,  # 6 месяцев
}

PRIOR_BY_EVIDENCE = {
    "empirical":      0.75,
    "documented":     0.65,
    "legal":          0.85,
    "knowledge":      0.60,
    "interpretation": 0.45,
    "inferred":       0.45,
    "generated":      0.25,
}

REFUTE_WEIGHT = {
    "legal":          2.5,
    "knowledge":      2.5,
    "empirical":      1.5,
    "documented":     1.5,
    "inferred":       1.0,
    "generated":      1.0,
    "interpretation": 1.0,
}

VALID_OUTCOMES       = {"success", "fail", "partial", "abandoned"}
VALID_EVIDENCE_TYPES = {"empirical", "documented", "legal", "knowledge",
                        "interpretation", "inferred", "generated"}
VALID_CATEGORIES     = {"rules", "memory", "infra", "deploy", "plan",
                        "write", "user", "dev", "test", "research", "knowledge"}

FLASHBACK_THRESHOLD      = 0.60
LESSON_CONF_THRESHOLD    = 0.75
LESSON_MASTERY_THRESHOLD = 0.60
NEEDS_REVIEW_THRESHOLD   = 0.40

# ─── Логирование ──────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / f"memory-reflect-{datetime.now().strftime('%Y-%m')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("memory-reflect")

# ─── Retry decorator ──────────────────────────────────────────────────────────

def with_retry(fn, attempts: int = RETRY_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY):
    """Повторяет fn при сетевых ошибках с экспоненциальной задержкой."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (ServiceUnavailable, TransientError, ConnectionError, OSError) as e:
            if attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(f"Attempt {attempt}/{attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s...")
            time.sleep(delay)

# ─── Байес ────────────────────────────────────────────────────────────────────

def get_decay_rate(evidence_type: str, category: str) -> float:
    if evidence_type in DECAY_BY_EVIDENCE_TYPE:
        return DECAY_BY_EVIDENCE_TYPE[evidence_type]
    return DECAY_BY_CATEGORY.get(category, 0.002534)


def get_prior(evidence_type: str) -> float:
    return PRIOR_BY_EVIDENCE.get(evidence_type, 0.40)


def bayesian_update(
    prior: float,
    event_type: str,
    conclusion_evidence_type: str,
    conclusion_category: str,
    event_ts: int,
    event_category: str = "",
) -> float:
    """
    Обновляет confidence.
    event_ts — время когда произошло событие (ts задачи из дампа).
    event_type: 'confirm' | 'refute'
    """
    now_ts  = datetime.now(timezone.utc).timestamp()
    days    = max((now_ts - event_ts) / 86400, 0)
    decay   = get_decay_rate(conclusion_evidence_type, conclusion_category)
    recency = math.exp(-decay * days)

    cross = bool(event_category and event_category != conclusion_category)

    if event_type == "confirm":
        w      = (1.5 if cross else 1.0) * recency
        p_e_h  = min(0.8 * w, 0.95)
        p_e_nh = max(0.2 * w, 0.05)
    else:
        rw     = REFUTE_WEIGHT.get(conclusion_evidence_type, 1.0)
        w      = rw * (2.0 if cross else 1.0) * recency
        p_e_h  = max(0.2 / max(w, 0.01), 0.05)
        p_e_nh = min(0.8 * w, 0.95)

    p_e = prior * p_e_h + (1 - prior) * p_e_nh
    if p_e < 1e-9:
        return prior

    return round(min(max((prior * p_e_h) / p_e, 0.05), 0.95), 3)

# ─── Category inference ───────────────────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "rules":    ["protocol", "rule", "soul", "principle", "guardrail",
                 "behavior", "протокол", "правило", "принцип"],
    "memory":   ["memory", "neo4j", "qdrant", "mem0", "reflection",
                 "schema", "память", "рефлексия", "схема"],
    "infra":    ["server", "port", "nginx", "network", "daemon",
                 "сервер", "порт", "сеть", "сервис"],
    "deploy":   ["deploy", "restart", "config", "docker", "container",
                 "systemd", "деплой", "контейнер", "конфиг"],
    "dev":      ["code", "fork", "branch", "git", "refactor", "bug",
                 "код", "форк", "ветка", "рефакторинг"],
    "test":     ["test", "validate", "check", "verify", "debug",
                 "тест", "проверка", "отладка"],
    "research": ["research", "find", "search", "analyze", "compare",
                 "исследование", "поиск", "анализ"],
    "knowledge":["learn", "understand", "concept", "how does",
                 "изучить", "понять", "концепция"],
    "write":    ["write", "document", "report", "instruction",
                 "написать", "документ", "отчёт"],
    "plan":     ["plan", "roadmap", "backlog", "schedule",
                 "план", "роадмап", "бэклог"],
    "user":     ["user", "олег", "напомни", "remind", "personal",
                 "личный", "пользователь"],
}


def infer_category(dump: dict) -> str:
    if "category" in dump and dump["category"] in VALID_CATEGORIES:
        return dump["category"]

    text = " ".join([
        dump.get("goal", ""),
        dump.get("reason", ""),
        dump.get("insight", ""),
    ]).lower()

    scores = {
        cat: sum(1 for kw in kws if kw in text)
        for cat, kws in CATEGORY_KEYWORDS.items()
    }

    best_cat   = max(scores, key=scores.get)
    best_score = scores[best_cat]

    if best_score == 0:
        goal_preview = dump.get('goal', '')[:50]
        log.warning(f"Category inference failed for: '{goal_preview}' → defaulting to 'dev'")
        return "dev"

    log.info(f"Category inferred: {best_cat} (score={best_score})")
    return best_cat


def fact_hash(task_id: str, fact: str) -> str:
    return hashlib.md5(f"{task_id}::{fact}".encode()).hexdigest()[:16]

# ─── Neo4j ────────────────────────────────────────────────────────────────────

class Neo4jStore:

    def __init__(self, uri: str, user: str, password: str, dry_run: bool = False):
        self.dry_run = dry_run
        if not dry_run:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
        log.info(f"Neo4j {'dry-run' if dry_run else 'connected'}: {uri}")

    def close(self):
        if not self.dry_run:
            self.driver.close()

    def run(self, query: str, **params) -> list:
        if self.dry_run:
            log.info(f"[DRY-RUN] Cypher: {query.split(chr(10))[0][:70]}...")
            return []

        def _run():
            with self.driver.session() as session:
                return [dict(r) for r in session.run(query, **params)]

        return with_retry(_run)

    # ── Schema ────────────────────────────────────────────────────────────────

    def init_schema(self):
        stmts = [
            "CREATE CONSTRAINT task_id_unique IF NOT EXISTS FOR (t:Task) REQUIRE t.task_id IS UNIQUE",
            "CREATE CONSTRAINT evidence_hash_unique IF NOT EXISTS FOR (e:Evidence) REQUIRE e.fact_hash IS UNIQUE",
            "CREATE CONSTRAINT conclusion_id_unique IF NOT EXISTS FOR (c:Conclusion) REQUIRE c.conclusion_id IS UNIQUE",
            "CREATE CONSTRAINT lesson_id_unique IF NOT EXISTS FOR (l:Lesson) REQUIRE l.lesson_id IS UNIQUE",
            "CREATE CONSTRAINT event_id_unique IF NOT EXISTS FOR (ev:Event) REQUIRE ev.event_id IS UNIQUE",
            "CREATE INDEX task_category IF NOT EXISTS FOR (t:Task) ON (t.category)",
            "CREATE INDEX task_outcome IF NOT EXISTS FOR (t:Task) ON (t.outcome)",
            "CREATE INDEX task_ts IF NOT EXISTS FOR (t:Task) ON (t.ts_end)",
            "CREATE INDEX conclusion_confidence IF NOT EXISTS FOR (c:Conclusion) ON (c.confidence)",
            "CREATE INDEX conclusion_category IF NOT EXISTS FOR (c:Conclusion) ON (c.category)",
            "CREATE INDEX conclusion_evidence_type IF NOT EXISTS FOR (c:Conclusion) ON (c.evidence_type)",
            "CREATE INDEX lesson_needs_review IF NOT EXISTS FOR (l:Lesson) ON (l.needs_review)",
            "CREATE INDEX lesson_mastery IF NOT EXISTS FOR (l:Lesson) ON (l.mastery)",
            "CREATE INDEX unknown_blocks IF NOT EXISTS FOR (u:Unknown) ON (u.blocks_next)",
        ]
        for s in stmts:
            self.run(s)
        log.info("Schema initialized")

    # ── Task ──────────────────────────────────────────────────────────────────

    def upsert_task(self, dump: dict, category: str) -> str:
        task_id = dump["task_id"]
        self.run("""
            MERGE (t:Task {task_id: $task_id})
            SET t.goal        = $goal,
                t.outcome     = $outcome,
                t.category    = $category,
                t.tags        = $tags,
                t.agent       = $agent,
                t.ts_end      = $ts_end,
                t.environment = $environment
        """,
            task_id     = task_id,
            goal        = dump.get("goal", ""),
            outcome     = dump.get("outcome", "unknown"),
            category    = category,
            tags        = dump.get("tags", []),
            agent       = dump.get("agent", "main"),
            ts_end      = dump.get("ts", int(datetime.now(timezone.utc).timestamp())),
            environment = dump.get("environment", ""),
        )
        log.info(f"Task: {task_id} category={category} outcome={dump.get('outcome')}")
        return task_id

    # ── Evidence ──────────────────────────────────────────────────────────────

    def upsert_evidence(self, task_id: str, dump: dict, category: str) -> str:
        """
        MERGE по fact_hash — идемпотентно.
        fact_hash = md5(task_id + reason)
        """
        reason        = dump.get("reason", "")
        evidence_type = dump.get("evidence_type", "inferred")
        fhash         = fact_hash(task_id, reason)
        evidence_id   = str(uuid.uuid5(uuid.NAMESPACE_DNS, fhash))
        event_ts      = dump.get("ts", int(datetime.now(timezone.utc).timestamp()))

        self.run("""
            MATCH (t:Task {task_id: $task_id})
            MERGE (e:Evidence {fact_hash: $fact_hash})
            ON CREATE SET
                e.evidence_id   = $evidence_id,
                e.fact          = $fact,
                e.evidence_type = $evidence_type,
                e.verified      = $verified,
                e.source        = $source,
                e.jurisdiction  = $jurisdiction,
                e.expires_ts    = $expires_ts,
                e.ts            = $ts
            MERGE (t)-[:HAS_EVIDENCE]->(e)
        """,
            task_id      = task_id,
            fact_hash    = fhash,
            evidence_id  = evidence_id,
            fact         = reason,
            evidence_type= evidence_type,
            verified     = evidence_type in {"empirical", "documented", "legal"},
            source       = dump.get("source", ""),
            jurisdiction = dump.get("jurisdiction", ""),
            expires_ts   = dump.get("expires_ts", 0),
            ts           = event_ts,
        )
        log.info(f"Evidence: {evidence_id} type={evidence_type}")
        return evidence_id

    # ── Conclusion ────────────────────────────────────────────────────────────

    def find_similar_conclusion(self, insight: str, category: str,
                                 qdrant=None) -> dict | None:
        """
        Семантический поиск через Qdrant (primary).
        Fallback: keyword matching в Neo4j.
        """
        # Primary: семантический поиск
        if qdrant:
            payload = qdrant.find_similar(insight, category)
            if payload and payload.get("conclusion_id"):
                conclusion_id = payload["conclusion_id"]
                results = self.run("""
                    MATCH (c:Conclusion {conclusion_id: $id})
                    RETURN c.conclusion_id AS id,
                           c.insight       AS insight,
                           c.confidence    AS confidence,
                           c.evidence_type AS evidence_type,
                           c.category      AS category
                """, id=conclusion_id)
                if results:
                    log.info(f"Semantic match → Neo4j: {conclusion_id}")
                    return results[0]

        # Fallback: keyword matching
        words = [
            w for w in insight.lower().split()
            if len(w) > 4 and w not in {
                "чтобы", "перед", "после", "через", "когда", "нужно",
                "should", "before", "after", "always", "never",
            }
        ][:3]

        if not words:
            return None

        for word in words:
            results = self.run("""
                MATCH (c:Conclusion {category: $category})
                WHERE toLower(c.insight) CONTAINS $word
                  AND c.confidence > 0.3
                RETURN c.conclusion_id AS id,
                       c.insight       AS insight,
                       c.confidence    AS confidence,
                       c.evidence_type AS evidence_type,
                       c.category      AS category
                ORDER BY c.confidence DESC
                LIMIT 1
            """, category=category, word=word)
            if results:
                log.info(f"Keyword fallback match '{word}': {results[0]['id']}")
                return results[0]

        return None

    def upsert_conclusion(self, task_id: str, evidence_id: str,
                          dump: dict, category: str, mem0_id: str) -> str:
        """
        MERGE по (task_id, insight_hash) — идемпотентно.
        """
        insight       = dump.get("insight", "")
        evidence_type = dump.get("evidence_type", "inferred")
        ihash         = fact_hash(task_id, insight)
        conclusion_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, ihash))
        prior         = get_prior(evidence_type)
        decay_rate    = get_decay_rate(evidence_type, category)
        event_ts      = dump.get("ts", int(datetime.now(timezone.utc).timestamp()))

        self.run("""
            MERGE (c:Conclusion {conclusion_id: $conclusion_id})
            ON CREATE SET
                c.insight       = $insight,
                c.applies_when  = $applies_when,
                c.evidence_type = $evidence_type,
                c.confidence    = $prior,
                c.category      = $category,
                c.decay_rate    = $decay_rate,
                c.mem0_id       = $mem0_id,
                c.ts_created    = $ts
        """,
            conclusion_id = conclusion_id,
            insight       = insight,
            applies_when  = dump.get("applies_when", ""),
            evidence_type = evidence_type,
            prior         = prior,
            category      = category,
            decay_rate    = decay_rate,
            mem0_id       = mem0_id,
            ts            = event_ts,
        )

        self.run("""
            MATCH (t:Task {task_id: $task_id})
            MATCH (c:Conclusion {conclusion_id: $conclusion_id})
            MERGE (t)-[:HAS_CONCLUSION]->(c)
        """, task_id=task_id, conclusion_id=conclusion_id)

        if evidence_id:
            self.run("""
                MATCH (e:Evidence {evidence_id: $evidence_id})
                MATCH (c:Conclusion {conclusion_id: $conclusion_id})
                MERGE (e)-[:SUPPORTS]->(c)
            """, evidence_id=evidence_id, conclusion_id=conclusion_id)

        log.info(f"Conclusion: {conclusion_id} conf={prior:.2f} "
                 f"type={evidence_type} decay={decay_rate:.6f}")
        return conclusion_id

    def update_conclusion_bayes(self, task_id: str, conclusion_id: str,
                                 dump: dict, event_type: str, category: str):
        existing = self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})
            RETURN c.confidence    AS confidence,
                   c.evidence_type AS evidence_type,
                   c.category      AS category
        """, id=conclusion_id)

        if not existing:
            log.warning(f"Conclusion not found: {conclusion_id}")
            return

        prior    = existing[0]["confidence"]
        ev_type  = existing[0]["evidence_type"]
        cat      = existing[0]["category"]
        event_ts = dump.get("ts", int(datetime.now(timezone.utc).timestamp()))

        posterior = bayesian_update(
            prior                    = prior,
            event_type               = event_type,
            conclusion_evidence_type = ev_type,
            conclusion_category      = cat,
            event_ts                 = event_ts,
            event_category           = category,
        )

        rel = "SUPPORTS" if event_type == "confirm" else "REFUTES"
        rw  = REFUTE_WEIGHT.get(ev_type, 1.0) if event_type == "refute" else 1.0

        self.run(f"""
            MATCH (c:Conclusion {{conclusion_id: $conclusion_id}})
            MATCH (t:Task {{task_id: $task_id}})
            SET c.confidence = $posterior
            CREATE (t)-[:{rel} {{outcome: $outcome, ts: $ts, weight: $weight}}]->(c)
        """,
            conclusion_id = conclusion_id,
            task_id       = task_id,
            posterior     = posterior,
            outcome       = dump.get("outcome", ""),
            ts            = event_ts,
            weight        = round(rw, 3),
        )
        log.info(f"Conclusion {conclusion_id} {rel}: {prior:.3f} → {posterior:.3f}")

        self._check_lesson(conclusion_id)
        self._check_needs_review(conclusion_id)

    # ── Lesson ────────────────────────────────────────────────────────────────

    def _check_lesson(self, conclusion_id: str):
        """Создать Lesson если confidence достиг порога."""
        results = self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})
            WHERE c.confidence >= $threshold
            RETURN c.conclusion_id AS id,
                   c.insight       AS insight,
                   c.applies_when  AS applies_when,
                   c.confidence    AS confidence
        """, id=conclusion_id, threshold=LESSON_CONF_THRESHOLD)

        if not results:
            return

        existing = self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})-[:GENERALIZES_TO]->(l:Lesson)
            RETURN l.lesson_id AS id LIMIT 1
        """, id=conclusion_id)

        if existing:
            return

        r         = results[0]
        lesson_id = str(uuid.uuid4())
        now       = int(datetime.now(timezone.utc).timestamp())

        self.run("""
            MATCH (c:Conclusion {conclusion_id: $conclusion_id})
            CREATE (l:Lesson {
                lesson_id:     $lesson_id,
                principle:     $principle,
                scope:         $scope,
                confidence:    $confidence,
                mastery:       0.0,
                applied_count: 0,
                needs_review:  false,
                affects_files: [],
                ts_created:    $ts
            })
            CREATE (c)-[:GENERALIZES_TO]->(l)
        """,
            conclusion_id = conclusion_id,
            lesson_id     = lesson_id,
            principle     = r["insight"],
            scope         = r["applies_when"],
            confidence    = r["confidence"],
            ts            = now,
        )
        log.info(f"Lesson created: {lesson_id} conf={r['confidence']:.3f}")

    def _check_needs_review(self, conclusion_id: str):
        self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})-[:GENERALIZES_TO]->(l:Lesson)
            WITH l, avg(c.confidence) AS avg_conf
            WHERE avg_conf < $threshold
            SET l.needs_review = true
        """, id=conclusion_id, threshold=NEEDS_REVIEW_THRESHOLD)

    def apply_lesson_by_principle(self, task_id: str,
                                   principle_text: str, helped: bool):
        """
        Ищет Lesson по тексту принципа (не по id).
        Friday не знает lesson_id — только текст.
        """
        words = [w for w in principle_text.lower().split() if len(w) > 4][:3]
        if not words:
            log.warning(f"Cannot find lesson — too short: '{principle_text[:50]}'")
            return

        results = self.run("""
            MATCH (l:Lesson)
            WHERE """ + " OR ".join(
                [f"toLower(l.principle) CONTAINS $w{i}"
                 for i in range(len(words))]
            ) + """
            RETURN l.lesson_id AS id LIMIT 1
        """, **{f"w{i}": w for i, w in enumerate(words)})

        if not results:
            log.warning(f"Lesson not found for: '{principle_text[:60]}'")
            return

        lesson_id = results[0]["id"]
        now       = int(datetime.now(timezone.utc).timestamp())

        self.run("""
            MATCH (t:Task {task_id: $task_id})
            MATCH (l:Lesson {lesson_id: $lesson_id})
            CREATE (t)-[:APPLIED_LESSON {helped: $helped, ts: $ts}]->(l)
            WITH l
            MATCH (:Task)-[r:APPLIED_LESSON]->(l)
            WITH l,
                 sum(CASE WHEN r.helped = true  THEN 1 ELSE 0 END) AS wins,
                 sum(CASE WHEN r.helped = false THEN 1 ELSE 0 END) AS fails,
                 count(r) AS total
            SET l.mastery       = CASE WHEN (wins + fails) > 0
                                       THEN toFloat(wins) / (wins + fails)
                                       ELSE 0.0 END,
                l.applied_count = total
        """,
            task_id   = task_id,
            lesson_id = lesson_id,
            helped    = helped,
            ts        = now,
        )
        log.info(f"Lesson {lesson_id} applied: helped={helped}")

    # ── Flashback ─────────────────────────────────────────────────────────────

    def flashback(self, category: str) -> list:
        return self.run("""
            MATCH (t:Task)-[:HAS_CONCLUSION]->(c:Conclusion)
            WHERE c.category  = $category
              AND c.confidence >= $threshold
            RETURN c.insight       AS insight,
                   c.applies_when  AS applies_when,
                   c.confidence    AS confidence,
                   c.evidence_type AS evidence_type,
                   t.outcome       AS example_outcome,
                   'conclusion'    AS source_type
            ORDER BY c.confidence DESC
            LIMIT 5

            UNION

            MATCH (c:Conclusion)-[:GENERALIZES_TO]->(l:Lesson)
            WHERE l.confidence  >= $lesson_conf
              AND l.mastery     >= $lesson_mastery
              AND l.needs_review = false
            RETURN l.principle  AS insight,
                   l.scope      AS applies_when,
                   l.confidence AS confidence,
                   'lesson'     AS evidence_type,
                   ''           AS example_outcome,
                   'lesson'     AS source_type
            ORDER BY l.mastery DESC
            LIMIT 3
        """,
            category       = category,
            threshold      = FLASHBACK_THRESHOLD,
            lesson_conf    = LESSON_CONF_THRESHOLD,
            lesson_mastery = LESSON_MASTERY_THRESHOLD,
        )

# ─── Qdrant semantic search ───────────────────────────────────────────────────

import requests as _requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams,
    PointStruct, Filter, FieldCondition, MatchValue,
)

QDRANT_HOST       = "192.168.1.145"
QDRANT_PORT       = 6333
QDRANT_COLLECTION = "reflections"   # новая коллекция — не трогаем memories
EMBED_URL         = "http://192.168.1.145:11435/api/embed"
EMBED_MODEL       = "bge-m3:latest"
SIMILARITY_THRESHOLD = 0.72


class QdrantSearch:
    """
    Семантический поиск и хранение выводов агента.
    Коллекция: reflections (1024d, Cosine).
    Embeddings: bge-m3 через ollama /api/embed.
    Не зависит от mem0.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._client: QdrantClient | None = None

        if not dry_run:
            try:
                self._client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
                self._ensure_collection()
                log.info(f"Qdrant connected: {QDRANT_HOST}:{QDRANT_PORT} "
                         f"collection={QDRANT_COLLECTION}")
            except Exception as e:
                log.warning(f"Qdrant init failed: {e}. Running without semantic search.")

    def _ensure_collection(self):
        existing = [c.name for c in self._client.get_collections().collections]
        if QDRANT_COLLECTION not in existing:
            self._client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
            )
            log.info(f"Collection created: {QDRANT_COLLECTION}")

    def _embed(self, text: str) -> list[float] | None:
        """Получить вектор от bge-m3 через ollama /api/embed."""
        def _call():
            resp = _requests.post(
                EMBED_URL,
                json={"model": EMBED_MODEL, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            # ollama /api/embed возвращает {"embeddings": [[...]]}
            embeddings = data.get("embeddings", [])
            if embeddings:
                return embeddings[0]
            # fallback для старых версий ollama
            return data.get("embedding", [])

        try:
            return with_retry(_call)
        except Exception as e:
            log.error(f"Embed failed: {e}")
            return None

    def add(self, text: str, metadata: dict) -> str:
        """
        Добавить вывод в коллекцию reflections.
        Возвращает point_id (uuid string).
        """
        if self.dry_run or not self._client:
            fake_id = str(uuid.uuid4())
            log.info(f"[DRY-RUN] Qdrant add: '{text[:50]}' → {fake_id}")
            return fake_id

        vector = self._embed(text)
        if not vector:
            return ""

        point_id = str(uuid.uuid4())

        def _upsert():
            self._client.upsert(
                collection_name=QDRANT_COLLECTION,
                points=[PointStruct(
                    id      = point_id,
                    vector  = vector,
                    payload = {"text": text, **metadata},
                )],
            )
            return point_id

        try:
            return with_retry(_upsert)
        except Exception as e:
            log.error(f"Qdrant upsert failed: {e}")
            return ""

    def find_similar(self, insight: str, category: str) -> dict | None:
        """
        Семантический поиск похожего вывода в reflections.
        Фильтр по category + порог score > SIMILARITY_THRESHOLD.
        Возвращает payload с conclusion_id если найдено.
        """
        if self.dry_run or not self._client:
            return None

        vector = self._embed(insight)
        if not vector:
            return None

        try:
            results = self._client.query_points(
                collection_name = QDRANT_COLLECTION,
                query           = vector,
                query_filter    = Filter(must=[
                    FieldCondition(
                        key   = "category",
                        match = MatchValue(value=category),
                    ),
                    FieldCondition(
                        key   = "level",
                        match = MatchValue(value="conclusion"),
                    ),
                ]),
                limit           = 1,
                with_payload    = True,
            ).points

            if not results:
                return None

            top = results[0]
            if top.score < SIMILARITY_THRESHOLD:
                log.info(f"Semantic search: best score {top.score:.3f} "
                         f"< threshold {SIMILARITY_THRESHOLD} — no match")
                return None

            log.info(f"Semantic match found: score={top.score:.3f} "
                     f"id={top.id}")
            return top.payload

        except Exception as e:
            log.warning(f"Qdrant search failed: {e}")
            return None

# ─── Валидация ────────────────────────────────────────────────────────────────

def validate_dump(dump: dict) -> tuple[list[str], dict]:
    """
    Валидирует дамп. Не мутирует оригинал.
    Возвращает (errors, cleaned_dump).
    """
    errors  = []
    cleaned = dict(dump)

    for field in ["task_id", "goal", "outcome", "reason", "insight"]:
        if not cleaned.get(field, "").strip():
            errors.append(f"Missing or empty: {field}")

    if cleaned.get("outcome") not in VALID_OUTCOMES:
        errors.append(f"Invalid outcome '{cleaned.get('outcome')}'. "
                      f"Must be: {sorted(VALID_OUTCOMES)}")

    ev = cleaned.get("evidence_type", "inferred")
    if ev not in VALID_EVIDENCE_TYPES:
        errors.append(f"Invalid evidence_type '{ev}' — defaulting to 'inferred'")
        cleaned["evidence_type"] = "inferred"

    return errors, cleaned

# ─── Основная логика ──────────────────────────────────────────────────────────

def process_dump(dump: dict, neo4j: Neo4jStore, qdrant: QdrantSearch):
    task_id  = dump["task_id"]
    outcome  = dump["outcome"]
    insight  = dump["insight"]
    category = infer_category(dump)

    log.info(f"Processing: {task_id} category={category} outcome={outcome}")

    # 1. Task — идемпотентно
    neo4j.upsert_task(dump, category)

    # 2. Evidence из reason — идемпотентно
    evidence_id = ""
    if dump.get("reason", "").strip():
        evidence_id = neo4j.upsert_evidence(task_id, dump, category)

    # 3. Найти похожий Conclusion (семантика → keyword fallback)
    similar = neo4j.find_similar_conclusion(insight, category, qdrant)

    if similar:
        event_type = "confirm" if outcome in {"success", "partial"} else "refute"
        neo4j.update_conclusion_bayes(
            task_id, similar["id"], dump, event_type, category
        )
    else:
        # Записать вектор в Qdrant reflections
        qdrant_text = (
            f"{insight}. "
            f"Контекст: {dump.get('reason', '')}. "
            f"Категория: {category}."
        )
        qdrant_id = qdrant.add(qdrant_text, {
            "level":         "conclusion",
            "task_id":       task_id,
            "category":      category,
            "evidence_type": dump.get("evidence_type", "inferred"),
            "outcome":       outcome,
        })

        # Создать Conclusion в Neo4j — идемпотентно
        conclusion_id = neo4j.upsert_conclusion(
            task_id, evidence_id, dump, category, qdrant_id
        )

        # Обновить payload в Qdrant с реальным conclusion_id
        if qdrant_id and conclusion_id and not qdrant.dry_run and qdrant._client:
            try:
                qdrant._client.set_payload(
                    collection_name = QDRANT_COLLECTION,
                    payload         = {"conclusion_id": conclusion_id},
                    points          = [qdrant_id],
                )
            except Exception as e:
                log.warning(f"Qdrant payload update failed: {e}")

    # 4. Применить lessons если есть
    for applied in dump.get("lessons_applied", []):
        principle = applied.get("principle", "")
        helped    = applied.get("helped", True)
        if principle:
            neo4j.apply_lesson_by_principle(task_id, principle, helped)

    log.info(f"Done: {task_id}")


def delete_dump(path: Path, dry_run: bool, confirmed: bool = True):
    if not confirmed:
        log.warning(f"Dump NOT deleted (write not confirmed): {path}")
        return
    if dry_run:
        log.info(f"[DRY-RUN] Would delete: {path}")
        return
    try:
        path.unlink()
        log.info(f"Dump deleted: {path}")
    except Exception as e:
        log.warning(f"Could not delete dump: {e}")

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OpenClaw memory-reflect v4")
    parser.add_argument("--dump",        help="Path to task dump JSON")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--flashback",   action="store_true")
    parser.add_argument("--category",    default="dev")
    args = parser.parse_args()

    neo4j  = Neo4jStore(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
                        dry_run=args.dry_run)
    qdrant = QdrantSearch(dry_run=args.dry_run)

    success = False
    try:
        if args.init_schema:
            neo4j.init_schema()
            success = True
            return

        if args.flashback:
            results = neo4j.flashback(args.category)
            print(f"\n[flashback · category={args.category}]\n")
            for r in results:
                print(f"  [{r['source_type']} · conf={r['confidence']:.2f}]")
                print(f"  {r['insight']}")
                if r.get("applies_when"):
                    print(f"  когда: {r['applies_when']}")
                print()
            success = True
            return

        if not args.dump:
            parser.error("--dump required")

        dump_path = Path(args.dump)
        if not dump_path.exists():
            log.error(f"Dump not found: {dump_path}")
            sys.exit(1)

        raw          = json.loads(dump_path.read_text(encoding="utf-8"))
        errors, dump = validate_dump(raw)

        if errors:
            for e in errors:
                log.error(f"Validation: {e}")
            sys.exit(1)

        process_dump(dump, neo4j, qdrant)
        success = True

    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        success = False
    finally:
        neo4j.close()

    if args.dump:
        delete_dump(Path(args.dump), args.dry_run, confirmed=success)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
