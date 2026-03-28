#!/usr/bin/env python3
"""
memory-reflect.py v4 — CLI для OpenClaw memory stack.

Использование:
    python3 memory-reflect.py --dump /path/to/task.json
    python3 memory-reflect.py --dump /path/to/task.json --dry-run
    python3 memory-reflect.py --init-schema
    python3 memory-reflect.py --flashback --category deploy
    python3 memory-reflect.py --flashback --focus "порты конфигурация"
    python3 memory-reflect.py --flashback --focus "логирование" --category dev
    python3 memory-reflect.py --reflect --dry-run
    python3 memory-reflect.py --status

Формат дампа:
{
    "task_id":        "uuid",
    "goal":           "что хотели сделать",
    "outcome":        "success|fail|partial|abandoned",
    "reason":         "почему такой outcome",
    "insight":        "что делать в следующий раз",
    "evidence_type":  "empirical|documented|legal|knowledge|inferred|generated",
    "ts":             1705312800,
    "category":       "deploy",           (опционально — иначе выводится)
    "tags":           ["port", "docker"], (опционально)
    "lessons_applied": [                  (опционально)
        {"principle": "текст урока", "helped": true}
    ]
}
"""

import argparse
import json
import sys
from pathlib import Path

from store import (
    Neo4jStore,
    QdrantSearch,
    LLMClient,
    infer_category,
    process_dump,
    validate_dump,
    log,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _print_flashback_neo4j(results: list, category: str):
    print(f"\n[flashback · category={category}]\n")
    if not results:
        print("  (нет результатов)")
        return
    for r in results:
        source = r.get("source_type", "?")
        conf   = r.get("confidence", 0)
        print(f"  [{source} · conf={conf:.2f}]")
        print(f"  {r['insight']}")
        if r.get("applies_when"):
            print(f"  когда: {r['applies_when']}")
        print()


def _print_flashback_focus(results: list, focus: str, category: str):
    print(f"\n[flashback · focus='{focus}' · category={category or 'any'}]\n")
    if not results:
        print("  (нет результатов)")
        return
    for r in results:
        print(f"  [score={r['_score']:.2f} · {r.get('category', '')} · {r.get('outcome', '')}]")
        print(f"  {r.get('text', '')}")
        print()


def _print_status(state: dict):
    from datetime import datetime, timezone
    last_run_ts = state.get("last_run_ts", 0)
    if last_run_ts:
        last_run = datetime.fromtimestamp(last_run_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    else:
        last_run = "никогда"

    print("\n[reflection state]\n")
    print(f"  conclusions since last run : {state.get('conclusions_since_last_run', 0)}")
    print(f"  last run                   : {last_run}")
    print(f"  total principles created   : {state.get('total_principles_created', 0)}")
    print(f"  total meta created         : {state.get('total_meta_created', 0)}")
    print()


def _delete_dump(path: Path, dry_run: bool, confirmed: bool = True):
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
    parser = argparse.ArgumentParser(
        description="OpenClaw memory-reflect v4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dump",        help="Path to task dump JSON")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Не писать в базы, только логировать")
    parser.add_argument("--init-schema", action="store_true",
                        help="Инициализировать схему Neo4j")
    parser.add_argument("--flashback",   action="store_true",
                        help="Показать релевантный опыт")
    parser.add_argument("--critique",    action="store_true",
                        help="Анализировать релевантность результатов (Критик)")
    parser.add_argument("--category",    default="",
                        help="Фильтр по категории")
    parser.add_argument("--focus",       default="",
                        help="Семантический поиск по теме (Qdrant + reranker)")
    parser.add_argument("--reflect",     action="store_true",
                        help="Запустить рефлексию вручную (Lesson → Principle → Meta)")
    parser.add_argument("--status",      action="store_true",
                        help="Показать состояние ReflectionState")
    args = parser.parse_args()

    neo4j   = Neo4jStore(dry_run=args.dry_run)
    qdrant  = QdrantSearch(dry_run=args.dry_run, neo4j_store=neo4j)
    success = False

    try:
        # ── init-schema ───────────────────────────────────────────────────────
        if args.init_schema:
            neo4j.init_schema()
            success = True
            return

        # ── status ────────────────────────────────────────────────────────────
        if args.status:
            state = neo4j.get_reflection_state()
            _print_status(state)
            success = True
            return

        # ── flashback ─────────────────────────────────────────────────────────
        if args.flashback:
            if args.focus:
                results = qdrant.flashback_focus(args.focus, args.category)
                _print_flashback_focus(results, args.focus, args.category)
                
                # Если запрошен critique — анализируем результаты
                if args.critique:
                    print("\n" + "="*80)
                    print("🔍 КРИТИК — Анализ релевантности")
                    print("="*80 + "\n")
                    
                    critique = qdrant.critique_results(args.focus, results)
                    
                    print(f"📊 Summary: {critique['summary']}")
                    print(f"🎯 Relevance Score: {critique['relevance_score']:.2f}/1.0")
                    print()
                    
                    print("💪 Strengths:")
                    for s in critique['strengths']:
                        print(f"   ✅ {s}")
                    print()
                    
                    print("⚠️  Weaknesses:")
                    for w in critique['weaknesses']:
                        print(f"   ❌ {w}")
                    print()
                    
                    print("💡 Recommendations:")
                    for r in critique['recommendations']:
                        print(f"   🔧 {r}")
                    print()
                    
                    print("📈 Metrics:")
                    print(f"   Coverage: concrete={critique['coverage']['concrete']}, abstract={critique['coverage']['abstract']}, total={critique['coverage']['total']}")
                    print(f"   Scores: avg={critique['metrics']['avg_score']:.3f}, max={critique['metrics']['max_score']:.3f}, min={critique['metrics']['min_score']:.3f}")
                    print(f"   Delta (rerank vs original): {critique['metrics']['avg_delta']:.3f}")
                    print()
            else:
                category = args.category or "dev"
                results  = neo4j.flashback(category)
                _print_flashback_neo4j(results, category)
            success = True
            return

        # ── reflect (ручной запуск) ───────────────────────────────────────────
        if args.reflect:
            llm   = LLMClient()
            stats = neo4j.reflect(llm)
            print(f"\n[reflect done]\n")
            print(f"  principles created : {stats['principles']}")
            print(f"  meta created       : {stats['meta']}")
            print()
            success = True
            return

        # ── dump ──────────────────────────────────────────────────────────────
        if not args.dump:
            parser.error("Нужен один из: --dump, --flashback, --reflect, --init-schema, --status")

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

    except KeyboardInterrupt:
        log.info("Interrupted")
        success = False
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        success = False
    finally:
        neo4j.close()

    if args.dump:
        _delete_dump(Path(args.dump), args.dry_run, confirmed=success)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
