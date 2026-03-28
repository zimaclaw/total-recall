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

        # Проверка на создание Lesson
        self._check_lesson(conclusion_id)

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

    def flashback(self, category: str) -> list:
        """
        Возвращает релевантный опыт по category.
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


# ─── QdrantSearch ─────────────────────────────────────────────────────────────

# Весa для уровней памяти по типу query
LEVEL_WEIGHTS_CONCRETE = {
    "conclusion": 1.0,  # конкретные факты — максимальный вес
    "lesson": 0.8,      # уроки — высокий вес
    "principle": 0.6,   # принципы — средний вес
    "meta": 0.4         # метапринципы — низкий вес
}

LEVEL_WEIGHTS_ABSTRACT = {
    "conclusion": 0.4,  # конкретные факты — низкий вес
    "lesson": 0.6,      # уроки — средний вес
    "principle": 0.8,   # принципы — высокий вес
    "meta": 1.0         # метапринципы — максимальный вес
}


class QdrantSearch:

    def __init__(self, dry_run: bool = False, neo4j_store: "Neo4jStore" = None):
        self.dry_run = dry_run
        self._client: Optional[QdrantClient] = None
        self._neo4j = neo4j_store  # Для поиска abstract (principle/meta)

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

    def _classify_query(self, query: str) -> str:
        """
        Определяем тип query:
        - concrete = конкретный технический вопрос (порт, конфиг, ошибка)
        - abstract = абстрактный вопрос (принципы, решения, подходы)
        """
        concrete_keywords = [
            "порт", "ошибка", "баг", "не работает", "как настроить",
            "конфиг", "файл", "команда", "скрипт", "docker", "nginx",
            "ollama", "endpoint", "api", "path", "directory", "бэкап",
            "research", "субагент", "пустые ответы", "таймаут"
        ]
        
        abstract_keywords = [
            "как принимать", "принципы", "подход", "стратегия",
            "эффективно", "лучше", "правила", "методология",
            "решения", "неопределённости", "качества", "ассистентка"
        ]
        
        query_lower = query.lower()
        
        concrete_score = sum(1 for kw in concrete_keywords if kw in query_lower)
        abstract_score = sum(1 for kw in abstract_keywords if kw in query_lower)
        
        return "abstract" if abstract_score > concrete_score else "concrete"

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
                json={"model": settings.embed_model, "prompt": text},
                timeout=30,
            )
            resp.raise_for_status()
            data       = resp.json()
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
        """
        Rerank кандидатов через cosine similarity с embeddings.
        Использует Ollama embeddings API (не specialized reranker).
        """
        if not candidates:
            return candidates

        # Получить embedding для query
        query_embedding = self._embed(query)
        if not query_embedding:
            log.warning("Rerank failed: couldn't get query embedding")
            return candidates

        # Cosine similarity функция
        def cosine_similarity(a, b):
            from math import sqrt
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sqrt(sum(x * x for x in a))
            norm_b = sqrt(sum(x * x for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

        # Получить embeddings для всех кандидатов и посчитать сходство
        scored = []
        for candidate in candidates:
            text = candidate.get("text", "")
            if not text:
                continue
            
            doc_embedding = self._embed(text)
            if doc_embedding:
                score = cosine_similarity(query_embedding, doc_embedding)
                payload = dict(candidate)
                payload["_score"] = round(score, 3)
                scored.append(payload)

        # Сортировка по убыванию сходства
        scored.sort(key=lambda x: x["_score"], reverse=True)
        log.info(f"Reranked {len(scored)} results with cosine similarity")
        return scored

    def _search_abstract_from_neo4j(self, category: str, limit: int) -> list:
        """
        Поиск abstract данных (principle/meta) из Neo4j по категории.
        
        ВНИМАНИЕ: Этот метод требует подключения к Neo4j, но QdrantSearch
        не имеет доступа к Neo4jStore. Поэтому пока возвращаем пустой список.
        
        Для полноценной реализации нужно:
        1. Передать Neo4jStore в QdrantSearch через конструктор
        2. Или создать отдельную функцию combine_flashback(neo4j, qdrant, query, category)
        """
        # TODO: Реализовать поиск в Neo4j
        # Пока возвращаем пустой список
        return []

    def flashback_focus(self, focus: str, category: str = "", limit: int = 5) -> list:
        """
        Flashback с двумя источниками:
        1. Qdrant — concrete данные (conclusion/lesson) через векторный поиск
        2. Neo4j — abstract данные (principle/meta) через категорийный поиск
        
        Возвращает комбинацию с балансом ~60/40 concrete/abstract
        """
        if self.dry_run or not self._client:
            return []

        vector = self._embed(focus)
        if not vector:
            return []

        # 1. Классифицируем query
        query_type = self._classify_query(focus)
        
        # 2. Фильтры для Qdrant (без фильтра по level!)
        must_filters = []
        if category:
            must_filters.append(
                FieldCondition(key="category", match=MatchValue(value=category))
            )

        try:
            # 3. Поиск в Qdrant (concrete данные)
            results = self._client.query_points(
                collection_name = settings.qdrant_collection,
                query           = vector,
                query_filter    = Filter(must=must_filters),
                limit           = 50,
                with_payload    = True,
            ).points

            concrete_candidates = []
            for r in results:
                if r.score < settings.similarity_threshold * 0.85:
                    continue
                    
                payload = dict(r.payload)
                level = payload.get("level", "conclusion")
                
                # Только conclusion и lesson из Qdrant
                if level not in ["conclusion", "lesson"]:
                    continue
                
                original_score = round(r.score, 3)
                weight = LEVEL_WEIGHTS_CONCRETE.get(level, 0.5)
                
                concrete_candidates.append({
                    **payload,
                    "original_score": original_score,
                    "_score": round(original_score * weight, 3),
                    "level_weight": weight,
                    "source": "qdrant",
                })

            # 4. Сортируем concrete по score
            concrete_candidates.sort(key=lambda x: x["_score"], reverse=True)

            # 5. Определяем баланс concrete/abstract
            # Для abstract query — больше abstract, для concrete — больше concrete
            if query_type == "abstract":
                concrete_limit = max(1, int(limit * 0.3))  # Уменьшил до 30%
                abstract_limit = limit - concrete_limit  # Увеличил до 70%
            else:
                concrete_limit = max(2, int(limit * 0.5))  # Уменьшил до 50%
                abstract_limit = limit - concrete_limit  # Увеличил до 50%
            
            # Берём top 10 из каждого источника для лучшего отбора через rerank
            QDRANT_TOP_N = 10
            NEO4J_TOP_N = 10
            
            # 6. Берём лучшие concrete (top 10 из Qdrant)
            selected_concrete = concrete_candidates[:QDRANT_TOP_N]
            
            # 7. Поиск из Neo4j (все уровни: conclusion/lesson/principle/meta)
            neo4j_candidates = []
            if self._neo4j:
                # Используем существующий метод flashback() из Neo4jStore
                # Он возвращает все уровни: conclusion, lesson, principle, meta
                neo4j_results = self._neo4j.flashback(category)
                
                for r in neo4j_results:
                    source = r.get("source_type", "")
                    # Используем все уровни из Neo4j
                    neo4j_candidates.append({
                        "text": r.get("insight", ""),
                        "level": source,
                        "category": r.get("category", category or "any"),
                        "confidence": r.get("confidence", 0),
                        "original_score": r.get("confidence", 0),
                        "_score": r.get("confidence", 0),  # Используем confidence как score
                        "source": "neo4j",
                        "applies_when": r.get("applies_when", ""),
                    })
            
            # 8. Берём лучшие из Neo4j (top 10)
            selected_neo4j = neo4j_candidates[:NEO4J_TOP_N]
            
            # 9. Объединяем Qdrant + Neo4j
            selected = selected_concrete + selected_neo4j
            
            if not selected:
                log.info(f"flashback_focus '{focus[:40]}' → 0 candidates")
                return []

            # 8. Rerank только Qdrant результаты (Neo4j уже отсортирован по confidence)
            reranked_qdrant = self._rerank(focus, selected_concrete)
            
            # Объединяем: reranked Qdrant + Neo4j (без rerank)
            combined = reranked_qdrant + selected_neo4j
            
            # Сортируем по score и выбираем топ-N
            combined.sort(key=lambda x: x.get("_score", 0), reverse=True)
            final_results = combined[:limit]
            
            log.info(f"flashback_focus '{focus[:40]}' type={query_type} cat={category or 'any'} "
                     f"qdrant={len(concrete_candidates)} neo4j={len(neo4j_candidates)} "
                     f"→ {len(final_results)} final (selected: {min(QDRANT_TOP_N, len(concrete_candidates))}+{min(NEO4J_TOP_N, len(neo4j_candidates))})")
            return final_results

        except Exception as e:
            log.warning(f"flashback_focus failed: {e}")
            return []

    def critique_results(self, focus: str, results: list) -> dict:
        """
        Критик — анализ релевантности результатов flashback
        
        Возвращает:
        {
            "summary": "краткий анализ",
            "strengths": ["сильные стороны"],
            "weaknesses": ["слабые стороны"],
            "recommendations": ["предложения по улучшению"],
            "relevance_score": 0.0-1.0,
            "coverage": {
                "concrete": count,
                "abstract": count,
                "total": count
            }
        }
        """
        if not results:
            return {
                "summary": "Нет результатов для анализа",
                "strengths": [],
                "weaknesses": ["Пустой результат flashback"],
                "recommendations": ["Проверить наличие данных в базе", "Увеличить threshold"],
                "relevance_score": 0.0,
                "coverage": {"concrete": 0, "abstract": 0, "total": 0}
            }
        
        # Анализ coverage
        concrete_count = sum(1 for r in results if r.get("level") in ["conclusion", "lesson"])
        abstract_count = sum(1 for r in results if r.get("level") in ["principle", "meta"])
        total = len(results)
        
        # Анализ scores
        scores = [r.get("_score", 0) for r in results]
        avg_score = sum(scores) / len(scores) if scores else 0
        max_score = max(scores) if scores else 0
        min_score = min(scores) if scores else 0
        
        # Анализ original_score vs rerank score (delta)
        deltas = []
        for r in results:
            original = r.get("original_score", 0)
            reranked = r.get("_score", 0)
            deltas.append(reranked - original)
        
        avg_delta = sum(deltas) / len(deltas) if deltas else 0
        
        # Сильные стороны
        strengths = []
        if avg_score > 0.5:
            strengths.append("Высокий средний score ({:.2f})".format(avg_score))
        if concrete_count > 0 and abstract_count > 0:
            strengths.append("Хороший баланс concrete/abstract ({}/{}".format(concrete_count, abstract_count))
        if max_score > 0.7:
            strengths.append("Есть очень релевантные результаты (max={:.2f})".format(max_score))
        
        # Слабые стороны
        weaknesses = []
        if avg_score < 0.4:
            weaknesses.append("Низкий средний score ({:.2f})".format(avg_score))
        if concrete_count == 0:
            weaknesses.append("Нет concrete результатов (conclusion/lesson)")
        if abstract_count == 0:
            weaknesses.append("Нет abstract результатов (principle/meta)")
        if max_score < 0.5:
            weaknesses.append("Нет высоко релевантных результатов (max={:.2f})".format(max_score))
        if avg_delta < -0.1:
            weaknesses.append("Rerank снижает релевантность (avg_delta={:.2f})".format(avg_delta))
        
        # Рекомендации
        recommendations = []
        if avg_score < 0.4:
            recommendations.append("Заполнить базу релевантными данными")
        if concrete_count == 0:
            recommendations.append("Добавить conclusion/lesson в базу")
        if abstract_count == 0:
            recommendations.append("Добавить principle/meta через рефлексию")
        if max_score < 0.5:
            recommendations.append("Проверить качество embeddings (bge-m3)")
        if avg_delta < -0.1:
            recommendations.append("Rerank ухудшает результаты — проверить cosine similarity")
        
        # Оценка релевантности (0.0-1.0)
        relevance_score = min(1.0, avg_score * 1.5)  # нормализация
        
        return {
            "summary": f"Анализ {total} результатов: avg_score={avg_score:.2f}, concrete={concrete_count}, abstract={abstract_count}",
            "strengths": strengths if strengths else ["Нет выраженных сильных сторон"],
            "weaknesses": weaknesses if weaknesses else ["Нет критических проблем"],
            "recommendations": recommendations if recommendations else ["Продолжить работу"],
            "relevance_score": round(relevance_score, 3),
            "coverage": {
                "concrete": concrete_count,
                "abstract": abstract_count,
                "total": total
            },
            "metrics": {
                "avg_score": round(avg_score, 3),
                "max_score": round(max_score, 3),
                "min_score": round(min_score, 3),
                "avg_delta": round(avg_delta, 3)
            }
        }

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
