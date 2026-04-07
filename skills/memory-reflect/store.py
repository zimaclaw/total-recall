"""
store.py — ядро OpenClaw memory stack.

Содержит:
  - утилиты: байесовское обновление, вывод категории, валидация дампа
  - Neo4jStore: граф памяти (Task → Evidence → Conclusion → Lesson → Principle → Meta)
  - QdrantSearch: семантический поиск + reranking

Импортируется из:
  - memory-reflect.py  (CLI)
  - memory-daemon.py   (демон рефлексии)
"""

import hashlib
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter,
    MatchValue, PointStruct, VectorParams,
)

from config import settings

# ─── Логирование ──────────────────────────────────────────────────────────────

settings.log_dir.mkdir(parents=True, exist_ok=True)
_log_file = settings.log_dir / f"memory-reflect-{datetime.now().strftime('%Y-%m')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_file),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("openclaw.memory")

# ─── Константы валидации ──────────────────────────────────────────────────────

VALID_OUTCOMES = {"success", "fail", "partial", "abandoned"}

VALID_EVIDENCE_TYPES = {
    "empirical", "documented", "legal",
    "knowledge", "interpretation", "inferred", "generated",
}

VALID_CATEGORIES = {
    "rules", "memory", "infra", "deploy", "plan",
    "write", "user", "dev", "test", "research", "knowledge",
}

# ─── Байесовские таблицы ──────────────────────────────────────────────────────

DECAY_BY_EVIDENCE_TYPE = {
    "legal":     0.001900,  # half-life ~1 год
    "knowledge": 0.000317,  # half-life ~6 лет
}

DECAY_BY_CATEGORY = {
    "rules":    0.000634,
    "memory":   0.000950,
    "infra":    0.001270,
    "deploy":   0.001900,
    "plan":     0.001900,
    "write":    0.001900,
    "user":     0.001900,
    "dev":      0.002534,
    "test":     0.002534,
    "research": 0.003800,
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

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "rules":     ["protocol", "rule", "soul", "principle", "guardrail",
                  "behavior", "протокол", "правило", "принцип"],
    "memory":    ["memory", "neo4j", "qdrant", "mem0", "reflection",
                  "schema", "память", "рефлексия", "схема"],
    "infra":     ["server", "port", "nginx", "network", "daemon",
                  "сервер", "порт", "сеть", "сервис"],
    "deploy":    ["deploy", "restart", "config", "docker", "container",
                  "systemd", "деплой", "контейнер", "конфиг"],
    "dev":       ["code", "fork", "branch", "git", "refactor", "bug",
                  "код", "форк", "ветка", "рефакторинг"],
    "test":      ["test", "validate", "check", "verify", "debug",
                  "тест", "проверка", "отладка"],
    "research":  ["research", "find", "search", "analyze", "compare",
                  "исследование", "поиск", "анализ"],
    "knowledge": ["learn", "understand", "concept", "how does",
                  "изучить", "понять", "концепция"],
    "write":     ["write", "document", "report", "instruction",
                  "написать", "документ", "отчёт"],
    "plan":      ["plan", "roadmap", "backlog", "schedule",
                  "план", "роадмап", "бэклог"],
    "user":      ["user", "олег", "напомни", "remind", "personal",
                  "личный", "пользователь"],
}

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def with_retry(fn, attempts: int = 3, base_delay: float = 2.0):
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


def fact_hash(task_id: str, fact: str) -> str:
    return hashlib.md5(f"{task_id}::{fact}".encode()).hexdigest()[:16]


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
    now_ts  = datetime.now(timezone.utc).timestamp()
    days    = max((now_ts - event_ts) / 86400, 0)
    decay   = get_decay_rate(conclusion_evidence_type, conclusion_category)
    recency = math.exp(-decay * days)
    cross   = bool(event_category and event_category != conclusion_category)

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
        log.warning(f"Category inference failed: '{dump.get('goal','')[:50]}' → 'dev'")
        return "dev"

    log.info(f"Category inferred: {best_cat} (score={best_score})")
    return best_cat


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


# ─── Neo4jStore ───────────────────────────────────────────────────────────────

class Neo4jStore:

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        if not dry_run:
            self.driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
        log.info(f"Neo4j {'dry-run' if dry_run else 'connected'}: {settings.neo4j_uri}")

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
            # существующие
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
            # Фаза 2: Principle и Meta
            "CREATE CONSTRAINT principle_id_unique IF NOT EXISTS FOR (p:Principle) REQUIRE p.principle_id IS UNIQUE",
            "CREATE CONSTRAINT meta_id_unique IF NOT EXISTS FOR (m:Meta) REQUIRE m.meta_id IS UNIQUE",
            "CREATE INDEX principle_category IF NOT EXISTS FOR (p:Principle) ON (p.category)",
            "CREATE INDEX principle_confidence IF NOT EXISTS FOR (p:Principle) ON (p.confidence)",
            "CREATE INDEX meta_confidence IF NOT EXISTS FOR (m:Meta) ON (m.confidence)",
            # ReflectionState — состояние демона
            "CREATE CONSTRAINT reflection_state_unique IF NOT EXISTS FOR (r:ReflectionState) REQUIRE r.id IS UNIQUE",
        ]
        for s in stmts:
            self.run(s)

        # Создать ReflectionState если не существует
        self.run("""
            MERGE (r:ReflectionState {id: 'singleton'})
            ON CREATE SET
                r.conclusions_since_last_run = 0,
                r.last_run_ts                = 0,
                r.total_principles_created   = 0,
                r.total_meta_created         = 0
        """)
        log.info("Schema initialized (including Principle, Meta, ReflectionState)")

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
                                 qdrant: Optional["QdrantSearch"] = None) -> Optional[dict]:
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

    def _create_conclusion_embedding(self, conclusion_id: str, insight: str):
        """Создать embedding для Conclusion если не существует."""
        # Проверить существует ли embedding
        existing = self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})
            RETURN c.embedding IS NOT NULL as has_embedding
        """, id=conclusion_id)
        
        if existing and existing[0]["has_embedding"]:
            return
        
        # Создать embedding через ollama
        try:
            import requests
            import os
            
            embed_url = os.getenv('OLLAMA_EMBEDDINGS_URL', 'http://192.168.1.145:11435/api/embeddings')
            model = os.getenv('OLLAMA_EMBEDDINGS_MODEL', 'bge-m3')
            
            response = requests.post(
                embed_url,
                json={"model": model, "prompt": insight},
                timeout=30
            )
            
            if response.status_code == 200:
                embedding = response.json()["embedding"]
                self.run("""
                    MATCH (c:Conclusion {conclusion_id: $id})
                    SET c.embedding = $embedding
                """, id=conclusion_id, embedding=embedding)
                log.debug(f"Created embedding for Conclusion: {conclusion_id[:8]}...")
            else:
                log.warning(f"Failed to create embedding: {response.status_code}")
        except Exception as e:
            log.warning(f"Failed to create embedding: {e}")

    def upsert_conclusion(self, task_id: str, evidence_id: str,
                          dump: dict, category: str, mem0_id: str) -> str:
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

        # Инкремент счётчика для триггера рефлексии
        self._increment_reflection_counter()

        # Создать embedding для Conclusion (если не существует)
        self._create_conclusion_embedding(conclusion_id, insight)

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
        results = self.run("""
            MATCH (c:Conclusion {conclusion_id: $id})
            WHERE c.confidence >= $threshold
            RETURN c.conclusion_id AS id,
                   c.insight       AS insight,
                   c.applies_when  AS applies_when,
                   c.confidence    AS confidence
        """, id=conclusion_id, threshold=settings.lesson_conf_threshold)

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
        """, id=conclusion_id, threshold=settings.needs_review_threshold)

    def apply_lesson_by_principle(self, task_id: str,
                                   principle_text: str, helped: bool):
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

    # ── ReflectionState ───────────────────────────────────────────────────────

    def _increment_reflection_counter(self):
        """Увеличивает счётчик новых Conclusion для триггера демона."""
        self.run("""
            MERGE (r:ReflectionState {id: 'singleton'})
            SET r.conclusions_since_last_run =
                coalesce(r.conclusions_since_last_run, 0) + 1
        """)

    def get_reflection_state(self) -> dict:
        results = self.run("""
            MATCH (r:ReflectionState {id: 'singleton'})
            RETURN r.conclusions_since_last_run AS conclusions_since_last_run,
                   r.last_run_ts                AS last_run_ts,
                   r.total_principles_created   AS total_principles_created,
                   r.total_meta_created         AS total_meta_created
        """)
        if results:
            return results[0]
        return {
            "conclusions_since_last_run": 0,
            "last_run_ts": 0,
            "total_principles_created": 0,
            "total_meta_created": 0,
        }

    def reset_reflection_counter(self):
        now = int(datetime.now(timezone.utc).timestamp())
        self.run("""
            MERGE (r:ReflectionState {id: 'singleton'})
            SET r.conclusions_since_last_run = 0,
                r.last_run_ts                = $now
        """, now=now)
        log.info("ReflectionState counter reset")

    def regenerate_embeddings(self, llm: "LLMClient"):
        """Перегенерировать embeddings для всех узлов в новом формате."""
        log.info("Starting embeddings regeneration...")

        with self.driver.session() as s:
            # Conclusion: "goal: {goal} | outcome: {outcome} | insight: {insight}"
            conclusions = s.run("""
                MATCH (t:Task)-[:HAS_CONCLUSION]->(c:Conclusion)
                RETURN c.conclusion_id AS id, t.goal AS goal, t.outcome AS outcome, c.insight AS insight
            """).data()

            for c in conclusions:
                text = f"goal: {c['goal']} | outcome: {c['outcome']} | insight: {c['insight']}"
                embedding = llm.embed(text)
                if embedding:
                    s.run("""
                        MATCH (c:Conclusion {conclusion_id: $id})
                        SET c.embedding = $embedding
                    """, id=c['id'], embedding=embedding)

            log.info(f"Conclusion: {len(conclusions)} embeddings updated")

            # Lesson: "lesson: {principle} | mastery: {mastery}"
            lessons = s.run("""
                MATCH (l:Lesson)
                RETURN l.lesson_id AS id, l.principle AS principle, l.mastery AS mastery
            """).data()

            for l in lessons:
                text = f"lesson: {l['principle']} | mastery: {l['mastery']}"
                embedding = llm.embed(text)
                if embedding:
                    s.run("""
                        MATCH (l:Lesson {lesson_id: $id})
                        SET l.embedding = $embedding
                    """, id=l['id'], embedding=embedding)

            log.info(f"Lesson: {len(lessons)} embeddings updated")

            # Principle: "principle: {statement} | category: {category}"
            principles = s.run("""
                MATCH (p:Principle)
                RETURN p.principle_id AS id, p.statement AS statement, p.category AS category
            """).data()

            for p in principles:
                text = f"principle: {p['statement']} | category: {p['category']}"
                embedding = llm.embed(text)
                if embedding:
                    s.run("""
                        MATCH (p:Principle {principle_id: $id})
                        SET p.embedding = $embedding
                    """, id=p['id'], embedding=embedding)

            log.info(f"Principle: {len(principles)} embeddings updated")

            # Meta: "meta: {statement}"
            metas = s.run("""
                MATCH (m:Meta)
                RETURN m.meta_id AS id, m.statement AS statement
            """).data()

            for m in metas:
                text = f"meta: {m['statement']}"
                embedding = llm.embed(text)
                if embedding:
                    s.run("""
                        MATCH (m:Meta {meta_id: $id})
                        SET m.embedding = $embedding
                    """, id=m['id'], embedding=embedding)

            log.info(f"Meta: {len(metas)} embeddings updated")

        log.info("Embeddings regeneration completed")

    # ── Reflect: Lesson → Principle → Meta ────────────────────────────────────

    def reflect(self, llm: "LLMClient") -> dict:
        """
        Основной метод рефлексии. Вызывается демоном.

        1. Находит кластеры Lesson без Principle
           (семантически похожие, confidence выше порога)
        2. Для каждого кластера просит LLM сформулировать Principle
        3. Поднимает Principle → Meta если накопилось достаточно

        Возвращает статистику: {'principles': N, 'meta': M}
        """
        stats = {"principles": 0, "meta": 0}

        # ── Шаг 1: Lesson → Principle ─────────────────────────────────────────
        clusters = self._find_lesson_clusters()
        log.info(f"reflect: найдено {len(clusters)} кластеров Lesson")

        for cluster in clusters:
            principle_text = llm.synthesize_principle(
                lessons=[l["principle"] for l in cluster["lessons"]],
                category=cluster["category"],
            )
            if not principle_text:
                log.warning(f"LLM не вернул принцип для кластера {cluster['category']}")
                continue

            principle_id = self._create_principle(
                cluster   = cluster,
                principle = principle_text,
            )
            if principle_id:
                stats["principles"] += 1
                log.info(f"Principle created: {principle_id} "
                         f"category={cluster['category']} "
                         f"from {len(cluster['lessons'])} lessons")

        # ── Шаг 2: Principle → Meta ───────────────────────────────────────────
        meta_clusters = self._find_principle_clusters()
        log.info(f"reflect: найдено {len(meta_clusters)} кластеров Principle")

        for cluster in meta_clusters:
            meta_text = llm.synthesize_meta(
                principles=[p["statement"] for p in cluster["principles"]],
            )
            if not meta_text:
                continue

            meta_id = self._create_meta(
                cluster = cluster,
                meta    = meta_text,
            )
            if meta_id:
                stats["meta"] += 1
                log.info(f"Meta created: {meta_id} "
                         f"from {len(cluster['principles'])} principles")

        # Обновить счётчик
        now = int(datetime.now(timezone.utc).timestamp())
        self.run("""
            MERGE (r:ReflectionState {id: 'singleton'})
            SET r.last_run_ts              = $now,
                r.conclusions_since_last_run = 0,
                r.total_principles_created = coalesce(r.total_principles_created, 0) + $p,
                r.total_meta_created       = coalesce(r.total_meta_created, 0)       + $m
        """, now=now, p=stats["principles"], m=stats["meta"])

        log.info(f"reflect done: principles={stats['principles']} meta={stats['meta']}")
        return stats

    def _find_lesson_clusters(self) -> list[dict]:
        """
        Находит группы Lesson без Principle, сгруппированные по category.
        Группа должна иметь >= principle_min_cluster участников
        и средний confidence >= principle_conf_threshold.

        Простая стратегия: группировка по category.
        Более сложная (семантическая) — через Qdrant, добавим в Фазе 3.
        """
        results = self.run("""
            MATCH (c:Conclusion)-[:GENERALIZES_TO]->(l:Lesson)
            WHERE NOT (l)-[:ABSTRACTED_TO]->(:Principle)
              AND l.needs_review = false
            WITH coalesce(l.category, c.category) AS category,
                 l.lesson_id  AS lesson_id,
                 l.principle  AS principle,
                 l.confidence AS confidence,
                 l.mastery    AS mastery
            WITH category,
                 collect({lesson_id: lesson_id, principle: principle,
                          confidence: confidence, mastery: mastery}) AS lessons,
                 avg(confidence) AS avg_conf
            WHERE size(lessons) >= $min_cluster
              AND avg_conf      >= $conf_threshold
              AND category IS NOT NULL
            RETURN category, lessons, avg_conf
            ORDER BY avg_conf DESC
        """,
            min_cluster    = settings.principle_min_cluster,
            conf_threshold = settings.principle_conf_threshold,
        )

        return [
            {"category": r["category"], "lessons": r["lessons"],
             "avg_conf": r["avg_conf"]}
            for r in results
        ]

    def _create_principle(self, cluster: dict, principle: str) -> Optional[str]:
        """Создаёт узел Principle и связывает с Lesson кластера."""
        principle_id = str(uuid.uuid4())
        now          = int(datetime.now(timezone.utc).timestamp())
        lesson_ids   = [l["lesson_id"] for l in cluster["lessons"]]

        self.run("""
            CREATE (p:Principle {
                principle_id: $principle_id,
                statement:    $statement,
                category:     $category,
                confidence:   $confidence,
                lesson_count: $lesson_count,
                ts_created:   $ts
            })
        """,
            principle_id = principle_id,
            statement    = principle,
            category     = cluster["category"],
            confidence   = round(cluster["avg_conf"], 3),
            lesson_count = len(cluster["lessons"]),
            ts           = now,
        )

        # Связать каждый Lesson с новым Principle
        for lesson_id in lesson_ids:
            self.run("""
                MATCH (l:Lesson {lesson_id: $lesson_id})
                MATCH (p:Principle {principle_id: $principle_id})
                MERGE (l)-[:ABSTRACTED_TO]->(p)
            """, lesson_id=lesson_id, principle_id=principle_id)

        return principle_id

    def _find_principle_clusters(self) -> list[dict]:
        """Находит группы Principle без Meta."""
        results = self.run("""
            MATCH (p:Principle)
            WHERE NOT (p)-[:ABSTRACTED_TO]->(:Meta)
            WITH collect({
                     principle_id: p.principle_id,
                     statement:    p.statement,
                     category:     p.category,
                     confidence:   p.confidence
                 }) AS principles,
                 avg(p.confidence) AS avg_conf
            WHERE size(principles) >= $min_cluster
            RETURN principles, avg_conf
        """,
            min_cluster = settings.meta_min_cluster,
        )

        return [
            {
                "principles": r["principles"],
                "avg_conf":   r["avg_conf"],
            }
            for r in results
        ]

    def _create_meta(self, cluster: dict, meta: str) -> Optional[str]:
        """Создаёт узел Meta и связывает с Principle кластера."""
        meta_id      = str(uuid.uuid4())
        now          = int(datetime.now(timezone.utc).timestamp())
        principle_ids = [p["principle_id"] for p in cluster["principles"]]

        self.run("""
            CREATE (m:Meta {
                meta_id:          $meta_id,
                statement:        $statement,
                confidence:       $confidence,
                principle_count:  $principle_count,
                ts_created:       $ts
            })
        """,
            meta_id         = meta_id,
            statement       = meta,
            confidence      = round(cluster["avg_conf"], 3),
            principle_count = len(cluster["principles"]),
            ts              = now,
        )

        for principle_id in principle_ids:
            self.run("""
                MATCH (p:Principle {principle_id: $principle_id})
                MATCH (m:Meta {meta_id: $meta_id})
                MERGE (p)-[:ABSTRACTED_TO]->(m)
            """, principle_id=principle_id, meta_id=meta_id)

        return meta_id

    # ── Flashback ─────────────────────────────────────────────────────────────

    def _lesson_absorbs_conclusion(self, lesson_text: str, conclusion_insight: str) -> bool:
        """
        Проверяет похожесть текста Lesson и Conclusion.
        Если Lesson выведен из одного источника и текст похож — Conclusion поглощён.
        """
        if not lesson_text or not conclusion_insight:
            return False
        # Простое сравнение: если 70%+ слов совпадают — считаем похожими
        l_words = set(lesson_text.lower().split())
        c_words = set(conclusion_insight.lower().split())
        if not c_words:
            return False
        overlap = len(l_words & c_words) / len(c_words)
        return overlap >= 0.70

    def flashback(self, category: str) -> list:
        """
        Legacy: возвращает релевантный опыт по category.
        Поднимается по всей иерархии: Conclusion → Lesson → Principle → Meta.
        """
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

            UNION

            MATCH (l:Lesson)-[:ABSTRACTED_TO]->(p:Principle)
            WHERE p.category   = $category
              AND p.confidence >= $lesson_conf
            RETURN p.statement  AS insight,
                   ''           AS applies_when,
                   p.confidence AS confidence,
                   'principle'  AS evidence_type,
                   ''           AS example_outcome,
                   'principle'  AS source_type
            ORDER BY p.confidence DESC
            LIMIT 2

            UNION

            MATCH (p:Principle)-[:ABSTRACTED_TO]->(m:Meta)
            WHERE m.confidence >= $lesson_conf
            RETURN m.statement  AS insight,
                   ''           AS applies_when,
                   m.confidence AS confidence,
                   'meta'       AS evidence_type,
                   ''           AS example_outcome,
                   'meta'       AS source_type
            ORDER BY m.confidence DESC
            LIMIT 1
        """,
            category       = category,
            threshold      = settings.flashback_threshold,
            lesson_conf    = settings.lesson_conf_threshold,
            lesson_mastery = settings.lesson_mastery_threshold,
        )

    def flashback_hierarchical(self, query: str, llm: "LLMClient") -> dict:
        """
        Иерархический flashback с vector search и заменой Conclusion на Lesson:
        1. Vector search по query → топ-5 Conclusion (similarity > 0.65)
        2. Граф вверх: Conclusion → Lesson → Principle → Meta
        3. Если Lesson поглощает Conclusion — заменяем его Lesson на том же месте
        4. Дедупликация Principle и Meta по ID
        5. Порядок вывода: items (Conclusion/Lesson вперемешку) → Principle → Meta
        """
        query_embedding = llm.embed(query)
        if not query_embedding:
            log.warning("Failed to embed query")
            return {}

        with self.driver.session() as s:
            results = s.run("""
                CALL db.index.vector.queryNodes('conclusion_embedding', 5, $embedding)
                YIELD node AS c, score
                WHERE score > 0.65
                OPTIONAL MATCH (c)-[:GENERALIZES_TO]->(l:Lesson)
                    WHERE l.confidence > 0.60
                OPTIONAL MATCH (l)-[:ABSTRACTED_TO]->(p:Principle)
                    WHERE p.confidence > 0.70
                OPTIONAL MATCH (p)-[:ABSTRACTED_TO]->(m:Meta)
                OPTIONAL MATCH (t:Task)-[:HAS_CONCLUSION]->(c)
                RETURN
                    c.conclusion_id AS c_id,
                    c.insight AS c_insight,
                    c.confidence AS c_conf,
                    c.category AS c_category,
                    t.goal AS c_goal,
                    t.outcome AS c_outcome,
                    score AS c_similarity,
                    l.lesson_id AS l_id,
                    l.principle AS l_text,
                    l.confidence AS l_conf,
                    l.mastery AS l_mastery,
                    l.applied_count AS l_applied,
                    p.principle_id AS p_id,
                    p.statement AS p_statement,
                    p.confidence AS p_conf,
                    m.meta_id AS m_id,
                    m.statement AS m_statement,
                    m.confidence AS m_conf
                ORDER BY score DESC
            """, embedding=query_embedding).data()

        if not results:
            log.info(f"No results found for flashback query: '{query[:50]}'")
            return {}

        max_similarity = max(r['c_similarity'] for r in results)

        # Строим упорядоченный список: каждый Conclusion либо сам либо заменён Lesson
        items = []  # список {'type': 'conclusion'|'lesson', ...}

        seen_c = set()
        seen_l = set()
        seen_p = set()
        seen_m = set()

        for r in results:
            if not r['c_id'] or r['c_id'] in seen_c:
                continue
            seen_c.add(r['c_id'])

            # Есть Lesson для этого Conclusion?
            if r['l_id'] and r['l_id'] not in seen_l:
                absorbed = self._lesson_absorbs_conclusion(r['l_text'], r['c_insight'])
                if absorbed:
                    # Заменяем Conclusion его Lesson на том же месте
                    seen_l.add(r['l_id'])
                    items.append({
                        'type': 'lesson',
                        'text': r['l_text'],
                        'confidence': r['l_conf'],
                        'mastery': r['l_mastery'],
                        'applied': r['l_applied'],
                        'similarity': r['c_similarity'],  # similarity от Conclusion
                    })
                else:
                    # Lesson есть но текст разный — показываем оба
                    items.append({
                        'type': 'conclusion',
                        'insight': r['c_insight'],
                        'goal': r['c_goal'],
                        'outcome': r['c_outcome'],
                        'confidence': r['c_conf'],
                        'similarity': r['c_similarity'],
                        'category': r['c_category'],
                    })
                    if r['l_id'] not in seen_l:
                        seen_l.add(r['l_id'])
                        items.append({
                            'type': 'lesson',
                            'text': r['l_text'],
                            'confidence': r['l_conf'],
                            'mastery': r['l_mastery'],
                            'applied': r['l_applied'],
                            'similarity': r['c_similarity'],
                        })
            else:
                # Нет Lesson — показываем Conclusion как есть
                items.append({
                    'type': 'conclusion',
                    'insight': r['c_insight'],
                    'goal': r['c_goal'],
                    'outcome': r['c_outcome'],
                    'confidence': r['c_conf'],
                    'similarity': r['c_similarity'],
                    'category': r['c_category'],
                })

        # Principle — топ-1 по conf
        principles = []
        for r in results:
            if r['p_id'] and r['p_id'] not in seen_p and not principles:
                seen_p.add(r['p_id'])
                principles.append({
                    'statement': r['p_statement'],
                    'confidence': r['p_conf'],
                })

        # Meta — только если max similarity > 0.80
        metas = []
        if max_similarity > 0.80:
            for r in results:
                if r['m_id'] and r['m_id'] not in seen_m and not metas:
                    seen_m.add(r['m_id'])
                    metas.append({
                        'statement': r['m_statement'],
                        'confidence': r['m_conf'],
                    })

        absorbed_count = sum(1 for i in items if i['type'] == 'lesson')
        log.info(f"Hierarchical flashback: {len(items)} items, {len(principles)} principles, "
                 f"{len(metas)} metas (absorbed: {absorbed_count})")

        return {
            'items': items,  # упорядоченный список Conclusion и Lesson вперемешку
            'principles': principles,
            'metas': metas,
            'max_similarity': max_similarity,
        }


# ─── QdrantSearch ─────────────────────────────────────────────────────────────

class QdrantSearch:

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._client: Optional[QdrantClient] = None

        if not dry_run:
            try:
                self._client = QdrantClient(
                    host=settings.qdrant_host,
                    port=settings.qdrant_port,
                )
                self._ensure_collection()
                log.info(f"Qdrant connected: {settings.qdrant_host}:{settings.qdrant_port} "
                         f"collection={settings.qdrant_collection}")
            except Exception as e:
                log.warning(f"Qdrant init failed: {e}. Running without semantic search.")

    def _ensure_collection(self):
        existing = [c.name for c in self._client.get_collections().collections]
        if settings.qdrant_collection not in existing:
            self._client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
            )
            log.info(f"Collection created: {settings.qdrant_collection}")

    def _embed(self, text: str) -> Optional[list[float]]:
        def _call():
            resp = requests.post(
                settings.embed_url,
                json={"model": settings.embed_model, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            data       = resp.json()
            embeddings = data.get("embeddings", [])
            if embeddings:
                return embeddings[0]
            return data.get("embedding", [])

        try:
            return with_retry(_call)
        except Exception as e:
            log.error(f"Embed failed: {e}")
            return None

    def add(self, text: str, metadata: dict) -> str:
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
                collection_name=settings.qdrant_collection,
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

    def _rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return candidates

        docs = [c.get("text", "") for c in candidates]
        try:
            resp = requests.post(
                settings.rerank_url,
                json={"model": settings.rerank_model, "query": query, "documents": docs},
                timeout=15,
            )
            resp.raise_for_status()
            data    = resp.json()
            results = data.get("results", [])
            if not results:
                return candidates

            scored = []
            for r in results:
                idx = r.get("index", 0)
                if idx < len(candidates):
                    payload          = dict(candidates[idx])
                    payload["_score"] = round(r.get("relevance_score", 0.0), 3)
                    scored.append(payload)

            scored.sort(key=lambda x: x["_score"], reverse=True)
            log.info(f"Reranked {len(scored)} results")
            return scored

        except Exception as e:
            log.warning(f"Rerank failed (fallback to vector scores): {e}")
            return candidates

    def flashback_focus(self, focus: str, category: str = "", limit: int = 5) -> list:
        if self.dry_run or not self._client:
            return []

        vector = self._embed(focus)
        if not vector:
            return []

        must_filters = [
            FieldCondition(key="level", match=MatchValue(value="conclusion")),
        ]
        if category:
            must_filters.append(
                FieldCondition(key="category", match=MatchValue(value=category))
            )

        try:
            results = self._client.query_points(
                collection_name = settings.qdrant_collection,
                query           = vector,
                query_filter    = Filter(must=must_filters),
                limit           = 20,
                with_payload    = True,
            ).points

            candidates = []
            for r in results:
                if r.score >= settings.similarity_threshold * 0.85:
                    payload          = dict(r.payload)
                    payload["_score"] = round(r.score, 3)
                    candidates.append(payload)

            if not candidates:
                log.info(f"flashback_focus '{focus[:40]}' → 0 candidates")
                return []

            reranked = self._rerank(focus, candidates)[:limit]
            log.info(f"flashback_focus '{focus[:40]}' cat={category or 'any'} "
                     f"candidates={len(candidates)} → {len(reranked)} after rerank")
            return reranked

        except Exception as e:
            log.warning(f"flashback_focus failed: {e}")
            return []

    def find_similar(self, insight: str, category: str) -> Optional[dict]:
        if self.dry_run or not self._client:
            return None

        vector = self._embed(insight)
        if not vector:
            return None

        try:
            results = self._client.query_points(
                collection_name = settings.qdrant_collection,
                query           = vector,
                query_filter    = Filter(must=[
                    FieldCondition(key="category", match=MatchValue(value=category)),
                    FieldCondition(key="level",    match=MatchValue(value="conclusion")),
                ]),
                limit           = 1,
                with_payload    = True,
            ).points

            if not results:
                return None

            top = results[0]
            if top.score < settings.similarity_threshold:
                log.info(f"Semantic search: best score {top.score:.3f} "
                         f"< threshold {settings.similarity_threshold} — no match")
                return None

            log.info(f"Semantic match found: score={top.score:.3f} id={top.id}")
            return top.payload

        except Exception as e:
            log.warning(f"Qdrant search failed: {e}")
            return None


# ─── LLMClient ────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Тонкая обёртка над ollama /api/chat для задач рефлексии.
    Используется только в reflect() — синтез Principle и Meta.
    """

    def __init__(self):
        self.url   = settings.ollama_url
        self.model = settings.reflect_model

    def embed(self, text: str) -> Optional[list[float]]:
        """Генерирует embedding через bge-m3."""
        try:
            resp = requests.post(
                settings.embed_url,
                json={"model": settings.embed_model, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            data       = resp.json()
            embeddings = data.get("embeddings", [])
            if embeddings:
                return embeddings[0]
            return data.get("embedding", [])
        except Exception as e:
            log.error(f"Embed failed: {e}")
            return None

    def _ask(self, prompt: str, max_tokens: int = 200) -> Optional[str]:
        try:
            resp = requests.post(
                self.url,
                json={
                    "model":  self.model,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.3,
                    },
                    "think":    False,  # отключить thinking mode (qwen3)
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            data    = resp.json()
            message = data.get("message", {})

            # qwen3 в thinking mode кладёт ответ в thinking, content пустой
            raw = (message.get("content") or message.get("thinking") or "").strip()
            log.debug(f"LLM raw response: '{raw[:200]}'")
            return raw if raw else None
        except KeyError as e:
            log.error(f"LLM response format unexpected: {e}. "
                      f"Full response: {resp.json()}")
            return None
        except Exception as e:
            log.error(f"LLM request failed: {e}")
            return None

    def synthesize_principle(self, lessons: list[str], category: str) -> Optional[str]:
        """
        Формулирует один принцип из списка уроков одной категории.
        Возвращает одно предложение — чёткий, actionable принцип.
        """
        lessons_text = "\n".join(f"- {l}" for l in lessons)
        prompt = (
            f"Ты агент памяти. Тебе дан список уроков из категории '{category}'.\n"
            f"Сформулируй ОДИН общий принцип который объединяет эти уроки.\n"
            f"Принцип должен быть:\n"
            f"- конкретным и actionable (что делать / не делать)\n"
            f"- одним предложением, не длиннее 20 слов\n"
            f"- на русском языке\n\n"
            f"Уроки:\n{lessons_text}\n\n"
            f"Принцип (только текст, без пояснений):"
        )
        result = self._ask(prompt, max_tokens=60)
        if result:
            # Убрать возможные кавычки и префиксы
            result = result.strip('"\'').split("\n")[0].strip()
            log.info(f"Principle synthesized: '{result[:80]}'")
        return result

    def synthesize_meta(self, principles: list[str]) -> Optional[str]:
        """
        Формулирует метапринцип из списка принципов разных категорий.
        """
        principles_text = "\n".join(f"- {p}" for p in principles)
        prompt = (
            f"Ты агент памяти. Тебе дан список принципов из разных областей.\n"
            f"Сформулируй ОДИН метапринцип — стратегическое правило более высокого уровня.\n"
            f"Метапринцип должен быть:\n"
            f"- применимым к широкому классу ситуаций\n"
            f"- одним предложением, не длиннее 20 слов\n"
            f"- на русском языке\n\n"
            f"Принципы:\n{principles_text}\n\n"
            f"Метапринцип (только текст, без пояснений):"
        )
        result = self._ask(prompt, max_tokens=60)
        if result:
            result = result.strip('"\'').split("\n")[0].strip()
            log.info(f"Meta synthesized: '{result[:80]}'")
        return result


# ─── process_dump ─────────────────────────────────────────────────────────────

def process_dump(dump: dict, neo4j: Neo4jStore, qdrant: QdrantSearch):
    """Основная логика записи дампа задачи в память."""
    task_id  = dump["task_id"]
    outcome  = dump["outcome"]
    insight  = dump["insight"]
    category = infer_category(dump)

    log.info(f"Processing: {task_id} category={category} outcome={outcome}")

    neo4j.upsert_task(dump, category)

    evidence_id = ""
    if dump.get("reason", "").strip():
        evidence_id = neo4j.upsert_evidence(task_id, dump, category)

    similar = neo4j.find_similar_conclusion(insight, category, qdrant)

    if similar:
        event_type = "confirm" if outcome in {"success", "partial"} else "refute"
        neo4j.update_conclusion_bayes(
            task_id, similar["id"], dump, event_type, category
        )
    else:
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

        conclusion_id = neo4j.upsert_conclusion(
            task_id, evidence_id, dump, category, qdrant_id
        )

        if qdrant_id and conclusion_id and not qdrant.dry_run and qdrant._client:
            try:
                qdrant._client.set_payload(
                    collection_name = settings.qdrant_collection,
                    payload         = {"conclusion_id": conclusion_id},
                    points          = [qdrant_id],
                )
            except Exception as e:
                log.warning(f"Qdrant payload update failed: {e}")

    for applied in dump.get("lessons_applied", []):
        principle = applied.get("principle", "")
        helped    = applied.get("helped", True)
        if principle:
            neo4j.apply_lesson_by_principle(task_id, principle, helped)

    log.info(f"Done: {task_id}")
