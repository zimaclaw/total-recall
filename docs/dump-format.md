# Dump format

## When to create a dump

After every completed task that involved an **action** — not after conversation.

**Write dump:**
- changed a config, started a service, deployed
- researched a topic and reached a conclusion
- debugged an issue, found the root cause

**Skip dump:**
- answered Oleg's question without action
- read a file without changes
- discussion without system changes

---

## Format

```json
{
    "task_id":       "$(uuidgen)",
    "goal":          "what you wanted to do — one sentence",
    "outcome":       "success|fail|partial|abandoned",
    "reason":        "why this outcome — be specific",
    "insight":       "what to do differently next time — one sentence",
    "evidence_type": "empirical|documented|legal|knowledge|inferred|generated",
    "ts":            1742000000
}
```

### Required fields

| field | type | description |
|-------|------|-------------|
| `task_id` | string | unique uuid for this task |
| `goal` | string | what you tried to do |
| `outcome` | enum | result of the task |
| `reason` | string | why this outcome happened |
| `insight` | string | what to do next time |
| `evidence_type` | enum | how you know this |
| `ts` | int | unix timestamp when task ended |

### Optional fields

| field | type | description |
|-------|------|-------------|
| `category` | enum | inferred automatically if omitted |
| `tags` | string[] | free-form tags |
| `environment` | string | which server / context |
| `lessons_applied` | array | lessons used during this task |

---

## outcome values

| value | meaning |
|-------|---------|
| `success` | task completed as intended |
| `fail` | task failed, know why |
| `partial` | partially done, know what's missing |
| `abandoned` | dropped, reason known |

---

## evidence_type values

| value | prior | when to use |
|-------|-------|-------------|
| `empirical` | 0.75 | you ran it and saw the result yourself |
| `documented` | 0.65 | found in official documentation |
| `legal` | 0.85 | law, regulation, normative act |
| `knowledge` | 0.60 | fundamental knowledge about a domain |
| `inferred` | 0.45 | concluded from observations, not directly verified |
| `generated` | 0.25 | model reasoning, not verified |

---

## lessons_applied

If you used knowledge from flashback during this task — add it:

```json
"lessons_applied": [
    {
        "principle": "check port availability via ss -tlnp before changing",
        "helped":    true
    }
]
```

`helped: true` → mastery increases
`helped: false` → mastery decreases, system learns the lesson needs revision

---

## Examples

### Success with lesson applied

```json
{
    "task_id":       "a1b2c3d4-...",
    "goal":          "change nginx port to 8090",
    "outcome":       "success",
    "reason":        "checked port availability first via ss -tlnp, port was free",
    "insight":       "always verify port availability before any network config change",
    "evidence_type": "empirical",
    "ts":            1742000200,
    "lessons_applied": [
        {
            "principle": "check port availability via ss -tlnp before changing",
            "helped":    true
        }
    ]
}
```

### Fail

```json
{
    "task_id":       "e5f6g7h8-...",
    "goal":          "change gateway port to 9000",
    "outcome":       "fail",
    "reason":        "port 9000 occupied by sshd, did not check before changing",
    "insight":       "check port availability via ss -tlnp before any port change",
    "evidence_type": "empirical",
    "ts":            1742000000
}
```

### Knowledge task

```json
{
    "task_id":       "i9j0k1l2-...",
    "goal":          "understand how Q4_K_XL quantization affects model quality",
    "outcome":       "success",
    "reason":        "studied quantization docs and benchmarks",
    "insight":       "Q4_K_XL gives best quality/size tradeoff for 27B models on limited VRAM",
    "evidence_type": "documented",
    "ts":            1742000300
}
```

---

## How to create and run

```bash
# Create dump
DUMP_FILE="/home/ironman/.openclaw/workspace/memory/dumps/$(date +%s).json"
cat > "$DUMP_FILE" << 'DUMP'
{
    "task_id":       "$(uuidgen)",
    "goal":          "...",
    "outcome":       "success",
    "reason":        "...",
    "insight":       "...",
    "evidence_type": "empirical",
    "ts":            $(date +%s)
}
DUMP

# Run reflector in background
cd /home/ironman/.openclaw/workspace/skills/memory-reflect
poetry run python memory-reflect.py --dump "$DUMP_FILE" &
```
