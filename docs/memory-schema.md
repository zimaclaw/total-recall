# Memory schema

## Neo4j graph

### Nodes

#### Task
Unit of experience. Created after every completed action.

```json
{
  "task_id":     "uuid",
  "goal":        "change gateway port to 9000",
  "outcome":     "success|fail|partial|abandoned",
  "category":    "infra",
  "tags":        ["port", "docker"],
  "agent":       "main",
  "ts_end":      1742000000,
  "environment": "192.168.1.181"
}
```

#### Evidence
A fact — why the outcome happened. Attached to Task.

```json
{
  "evidence_id":   "uuid",
  "fact_hash":     "md5(task_id + fact)",
  "fact":          "port 9000 occupied by sshd",
  "evidence_type": "empirical",
  "verified":      true,
  "source":        "",
  "jurisdiction":  "",
  "expires_ts":    0,
  "ts":            1742000000
}
```

#### Conclusion
A lesson learned — what to do differently. Bayesian confidence.

```json
{
  "conclusion_id": "uuid",
  "insight":       "check port availability via ss -tlnp before changing",
  "applies_when":  "any network config change",
  "evidence_type": "empirical",
  "confidence":    0.75,
  "category":      "infra",
  "decay_rate":    0.001270,
  "mem0_id":       "qdrant-point-uuid",
  "ts_created":    1742000000
}
```

#### Lesson
A principle — generalized from multiple Conclusions.
Has two independent metrics.

```json
{
  "lesson_id":     "uuid",
  "principle":     "check port availability before any network change",
  "scope":         "network config changes",
  "confidence":    0.775,
  "mastery":       0.0,
  "applied_count": 0,
  "needs_review":  false,
  "affects_files": [],
  "ts_created":    1742000000
}
```

| metric | meaning |
|--------|---------|
| `confidence` | how certain the principle is correct (from Evidence via Bayes) |
| `mastery` | how consistently the agent applies it (from APPLIED_LESSON edges) |

#### Unknown
A knowledge gap — something not understood during the task.

```json
{
  "question":            "why does module A conflict with main branch",
  "priority":            "low",
  "blocks_next":         false,
  "resolved":            false,
  "resolved_by_task_id": null,
  "ts":                  1742000000
}
```

#### Event
Historical fact. Outside the Bayesian system — no confidence, just verified or not.

```json
{
  "event_id": "uuid",
  "name":     "World War II started",
  "date":     "1939-09-01",
  "verified": true,
  "sources":  ["britannica.com/..."]
}
```

---

### Edges

| edge | from → to | meaning |
|------|-----------|---------|
| `HAS_EVIDENCE` | Task → Evidence | task produced this fact |
| `HAS_EVIDENCE` | Iteration → Evidence | step produced this fact |
| `HAS_CONCLUSION` | Task → Conclusion | task produced this conclusion |
| `HAS_UNKNOWN` | Task → Unknown | task left this question open |
| `RELATED_TO` | Task → Task | related tasks |
| `SUPPORTS` | Evidence → Conclusion | fact supports this conclusion |
| `REFUTES` | Evidence → Conclusion | fact contradicts this conclusion |
| `GENERALIZES_TO` | Conclusion → Lesson | conclusion becomes a principle |
| `APPLIED_LESSON` | Task → Lesson | agent used this lesson (with `helped: bool`) |
| `INTERPRETS` | Conclusion → Event | conclusion is an interpretation of a historical fact |

---

## Bayesian confidence update

### Rule

```
evidence_type ∈ {legal, knowledge}  →  decay from DECAY_BY_EVIDENCE_TYPE
otherwise                           →  decay from DECAY_BY_CATEGORY
```

### Prior by evidence_type

| evidence_type | prior |
|---------------|-------|
| `legal` | 0.85 |
| `empirical` | 0.75 |
| `documented` | 0.65 |
| `knowledge` | 0.60 |
| `interpretation` | 0.45 |
| `inferred` | 0.45 |
| `generated` | 0.25 |

### Decay half-life by category

| category | half-life |
|----------|-----------|
| `knowledge` | 6 years |
| `rules` | 3 years |
| `memory` | 2 years |
| `infra` | 1.5 years |
| `deploy`, `plan`, `write`, `user` | 1 year |
| `legal` | 1 year |
| `dev`, `test` | 9 months |
| `research` | 6 months |

### Refute weight by evidence_type

| evidence_type | refute weight |
|---------------|---------------|
| `legal`, `knowledge` | 2.5× |
| `empirical`, `documented` | 1.5× |
| others | 1.0× |

Cross-category bonus: confirm +50%, refute +100%.

### Thresholds

| threshold | value | meaning |
|-----------|-------|---------|
| flashback | 0.60 | minimum confidence to appear in flashback |
| lesson creation | 0.75 | confidence to promote Conclusion → Lesson |
| lesson mastery | 0.60 | minimum mastery to appear in flashback |
| needs_review | 0.40 | avg confidence of base Conclusions falls below → flag Lesson |

---

## Qdrant collections

| collection | purpose |
|------------|---------|
| `memories` | user facts — managed by mem0, autoCapture/autoRecall |
| `reflections` | agent experience — managed by memory-reflect.py |

Both use `bge-m3:latest` embeddings, 1024 dimensions, Cosine distance.
