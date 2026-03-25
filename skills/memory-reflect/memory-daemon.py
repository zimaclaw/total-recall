#!/usr/bin/env python3
"""
memory-daemon.py — демон рефлексии OpenClaw.

Запускает reflect() по комбо-триггеру:
  - накопилось REFLECT_TRIGGER_COUNT новых Conclusion
  - ИЛИ прошло REFLECT_TRIGGER_HOURS часов без рефлексии

Изолирован от основного стека — падение демона не влияет на flashback и dump.

Запуск:
    python3 memory-daemon.py

Как сервис (systemd):
    см. memory-daemon.service рядом с этим файлом
"""

import asyncio
import signal
from datetime import datetime, timezone

from config import settings
from store import LLMClient, Neo4jStore, log


# ─── Триггер ──────────────────────────────────────────────────────────────────

def _should_reflect(state: dict) -> tuple[bool, str]:
    count    = state.get("conclusions_since_last_run", 0)
    last_run = state.get("last_run_ts", 0)
    now      = datetime.now(timezone.utc).timestamp()

    if count >= settings.reflect_trigger_count:
        return True, f"conclusions={count} >= trigger={settings.reflect_trigger_count}"

    if last_run == 0:
        return True, "первый запуск — никогда не запускался"

    hours_since = (now - last_run) / 3600
    if hours_since >= settings.reflect_trigger_hours:
        return True, f"прошло {hours_since:.1f}h >= trigger={settings.reflect_trigger_hours}h"

    return False, (
        f"conclusions={count}/{settings.reflect_trigger_count} · "
        f"{hours_since:.1f}h/{settings.reflect_trigger_hours}h"
    )


# ─── Основной цикл ────────────────────────────────────────────────────────────

async def reflection_loop(neo4j: Neo4jStore, llm: LLMClient,
                           shutdown: asyncio.Event):
    log.info(
        f"Daemon started · poll={settings.reflect_poll_seconds}s "
        f"· trigger: {settings.reflect_trigger_count} conclusions "
        f"OR {settings.reflect_trigger_hours}h"
    )

    loop = asyncio.get_running_loop()

    while not shutdown.is_set():
        try:
            state              = neo4j.get_reflection_state()
            should_run, reason = _should_reflect(state)

            if should_run:
                log.info(f"Trigger fired: {reason}")
                stats = await loop.run_in_executor(None, neo4j.reflect, llm)
                log.info(
                    f"Reflect complete: "
                    f"principles={stats['principles']} meta={stats['meta']}"
                )
            else:
                log.debug(f"No trigger: {reason}")

        except Exception as e:
            log.error(f"Reflect cycle error: {e}", exc_info=True)

        # Ждём poll или shutdown — что раньше
        try:
            await asyncio.wait_for(
                shutdown.wait(),
                timeout=settings.reflect_poll_seconds,
            )
        except asyncio.TimeoutError:
            pass


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    shutdown = asyncio.Event()
    loop     = asyncio.get_running_loop()

    # loop.add_signal_handler — правильный способ для asyncio
    # в отличие от signal.signal() корректно прерывает await
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: (
                log.info(f"Signal {s.name} received — shutting down"),
                shutdown.set(),
            ),
        )

    neo4j = Neo4jStore(dry_run=False)
    llm   = LLMClient()

    try:
        await reflection_loop(neo4j, llm, shutdown)
    finally:
        neo4j.close()
        log.info("Daemon stopped")


if __name__ == "__main__":
    asyncio.run(main())
