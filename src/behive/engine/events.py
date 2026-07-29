from __future__ import annotations
import logging
"""
hive2_events.py — HIVE 2.0 Event Bus & SSE Streaming Infrastructure


log = logging.getLogger(__name__)
Provides:
  - hive_events DuckDB table DDL + management
  - emit_event()       — write a structured event to the DB
  - get_events()       — query events for a mission (since_id pagination)
  - get_latest_quality() — fetch last quality_metrics for a mission
  - sse_stream()       — generator yielding SSE-formatted strings (polling)
  - FastAPI router     — GET /missions/{mission_id}/events  (text/event-stream)

Usage (standalone test):
  python3 hive2_events.py --test
"""


import json
import time
import sys
from typing import Generator, Optional

DB_PATH = ""  # Legacy — PostgreSQL is primary

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_EVENTS_DDL = """
CREATE SEQUENCE IF NOT EXISTS hive_events_seq START 1;
CREATE TABLE IF NOT EXISTS hive_events (
    id               INTEGER DEFAULT nextval('hive_events_seq') PRIMARY KEY,
    mission_id       VARCHAR NOT NULL,
    phase            VARCHAR NOT NULL,
    event_type       VARCHAR NOT NULL,
    data             JSON,
    quality_coverage FLOAT DEFAULT NULL,
    quality_depth    FLOAT DEFAULT NULL,
    quality_confidence FLOAT DEFAULT NULL,
    ts               TIMESTAMP DEFAULT NOW()
);
"""

_QUALITY_COLS_DDL = """
ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS quality_coverage   FLOAT DEFAULT NULL;
ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS quality_depth      FLOAT DEFAULT NULL;
ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS quality_confidence FLOAT DEFAULT NULL;
"""

# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str = DB_PATH):
    """Return a duckdb connection (read-write)."""
    _hive_db = __import__("hive2_db")
    return _hive_db.connect(read_only=False)


def ensure_events_table(db_path: str = DB_PATH) -> None:
    """Create hive_events table + sequence if they don't exist.
    Also adds quality_metrics columns to hive_missions if present.
    """
    con = _connect(db_path)
    try:
        # Create sequence first (must be separate from table DDL in DuckDB)
        con.execute("CREATE SEQUENCE IF NOT EXISTS hive_events_seq START 1")
        con.execute("""
            CREATE TABLE IF NOT EXISTS hive_events (
                id               INTEGER DEFAULT nextval('hive_events_seq') PRIMARY KEY,
                mission_id       VARCHAR NOT NULL,
                phase            VARCHAR NOT NULL,
                event_type       VARCHAR NOT NULL,
                data             JSON,
                quality_coverage FLOAT DEFAULT NULL,
                quality_depth    FLOAT DEFAULT NULL,
                quality_confidence FLOAT DEFAULT NULL,
                ts               TIMESTAMP DEFAULT NOW()
            )
        """)
        con.commit()

        # Migrate hive_missions with quality columns (best-effort)
        try:
            existing = {
                row[0].lower()
                for row in con.execute("DESCRIBE hive_missions").fetchall()
            }
            for col in ('quality_coverage', 'quality_depth', 'quality_confidence'):
                if col not in existing:
                    con.execute(f"ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS {col} FLOAT DEFAULT NULL")
            con.commit()
        except Exception:
            pass  # hive_missions may not exist yet — that's fine

    finally:
        con.close()


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------

def emit_event(
    db_path: str,
    mission_id: str,
    phase: str,
    event_type: str,
    data: Optional[dict] = None,
    coverage: Optional[float] = None,
    depth: Optional[float] = None,
    confidence: Optional[float] = None,
) -> int:
    """Write one event to hive_events. Returns the new event id."""
    data_json = json.dumps(data) if data is not None else None

    con = _connect(db_path)
    try:
        result = con.execute(
            """
            INSERT INTO hive_events
                (mission_id, phase, event_type, data,
                 quality_coverage, quality_depth, quality_confidence, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
            RETURNING id
            """,
            [mission_id, phase, event_type, data_json,
             coverage, depth, confidence]
        ).fetchone()
        con.commit()
        return result[0] if result else -1
    finally:
        con.close()


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------

def get_events(
    db_path: str,
    mission_id: str,
    since_id: int = 0,
) -> list[dict]:
    """Return events for a mission newer than since_id, ordered by id asc."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT id, mission_id, phase, event_type, data,
                   quality_coverage, quality_depth, quality_confidence, ts
            FROM hive_events
            WHERE mission_id = ? AND id > ?
            ORDER BY id ASC
            """,
            [mission_id, since_id]
        ).fetchall()
    finally:
        con.close()

    events = []
    for row in rows:
        eid, mid, phase, etype, data_raw, cov, dep, conf, ts = row
        data_parsed = None
        if data_raw:
            try:
                data_parsed = json.loads(data_raw) if isinstance(data_raw, str) else data_raw
            except (json.JSONDecodeError, TypeError):
                data_parsed = data_raw

        events.append({
            'id': eid,
            'mission_id': mid,
            'phase': phase,
            'event_type': etype,
            'data': data_parsed,
            'quality_coverage': cov,
            'quality_depth': dep,
            'quality_confidence': conf,
            'ts': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
        })
    return events


# ---------------------------------------------------------------------------
# get_latest_quality
# ---------------------------------------------------------------------------

def get_latest_quality(db_path: str, mission_id: str) -> dict:
    """Return the last quality metrics (coverage, depth, confidence) for a mission.
    Returns dict with keys: coverage, depth, confidence (float or None).
    """
    con = _connect(db_path)
    try:
        row = con.execute(
            """
            SELECT quality_coverage, quality_depth, quality_confidence
            FROM hive_events
            WHERE mission_id = ?
              AND (quality_coverage IS NOT NULL
                   OR quality_depth IS NOT NULL
                   OR quality_confidence IS NOT NULL)
            ORDER BY id DESC
            LIMIT 1
            """,
            [mission_id]
        ).fetchone()
    finally:
        con.close()

    if row:
        return {'coverage': row[0], 'depth': row[1], 'confidence': row[2]}
    return {'coverage': None, 'depth': None, 'confidence': None}


# ---------------------------------------------------------------------------
# SSE generator
# ---------------------------------------------------------------------------

def sse_stream(
    db_path: str,
    mission_id: str,
    poll_interval: float = 0.5,
) -> Generator[str, None, None]:
    """Generator yielding SSE-formatted strings by polling hive_events.

    Stops when an event with event_type='done' or event_type='error' is
    received, or when no new events arrive for 5 minutes (timeout).

    Yields lines like:  'data: {"id": 1, ...}\\n\\n'
    """
    last_id = 0
    idle_rounds = 0
    max_idle = int(300 / poll_interval)  # 5-minute timeout

    while True:
        new_events = get_events(db_path, mission_id, since_id=last_id)

        if new_events:
            idle_rounds = 0
            for ev in new_events:
                last_id = ev['id']
                payload = json.dumps(ev, default=str)
                yield f"data: {payload}\n\n"

                # Terminate on terminal event types
                if ev.get('event_type') in ('done', 'error'):
                    return
        else:
            idle_rounds += 1
            if idle_rounds >= max_idle:
                # Emit a timeout sentinel and stop
                timeout_ev = {
                    'id': -1,
                    'mission_id': mission_id,
                    'phase': 'done',
                    'event_type': 'error',
                    'data': {'error': 'SSE stream timed out — no events for 5 minutes'},
                    'quality_coverage': None,
                    'quality_depth': None,
                    'quality_confidence': None,
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                }
                yield f"data: {json.dumps(timeout_ev)}\n\n"
                return

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# FastAPI SSE endpoint (optional — included if FastAPI is available)
# ---------------------------------------------------------------------------

try:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import StreamingResponse

    router = APIRouter()

    @router.get(
        "/missions/{mission_id}/events",
        summary="Stream SSE events for a HIVE mission",
        response_class=StreamingResponse,
    )
    async def mission_events_sse(mission_id: str, since_id: int = 0):
        """
        Server-Sent Events stream for a HIVE mission.

        Streams `hive_events` rows as SSE `data:` lines.
        Terminates when event_type=done|error is received or after 5 min idle.

        Query params:
          since_id (int, default 0) — start from this event id
        """
        import asyncio

        async def _async_generator():
            loop = asyncio.get_event_loop()
            last_id_box = [since_id]
            idle_rounds = 0
            max_idle = int(300 / 0.5)

            while True:
                new_events = await loop.run_in_executor(
                    None, get_events, DB_PATH, mission_id, last_id_box[0]
                )

                if new_events:
                    idle_rounds = 0
                    for ev in new_events:
                        last_id_box[0] = ev['id']
                        payload = json.dumps(ev, default=str)
                        yield f"data: {payload}\n\n"
                        if ev.get('event_type') in ('done', 'error'):
                            return
                else:
                    idle_rounds += 1
                    if idle_rounds >= max_idle:
                        timeout_ev = {
                            'id': -1,
                            'mission_id': mission_id,
                            'phase': 'done',
                            'event_type': 'error',
                            'data': {'error': 'SSE stream timed out'},
                            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        }
                        yield f"data: {json.dumps(timeout_ev)}\n\n"
                        return

                await asyncio.sleep(0.5)

        return StreamingResponse(
            _async_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    _FASTAPI_AVAILABLE = True

except ImportError:
    _FASTAPI_AVAILABLE = False
    router = None  # type: ignore


# ---------------------------------------------------------------------------
# __main__ — self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='hive2_events self-test')
    parser.add_argument('--test', action='store_true', help='Run self-test')
    parser.add_argument('--db', default=DB_PATH, help='DuckDB path')
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        sys.exit(0)

    db = args.db
    test_mission = f'test_mission_{int(time.time())}'

    print(f'[hive2_events] Self-test starting — db={db}')
    print(f'[hive2_events] test mission_id={test_mission}')

    # 1. ensure_events_table
    print('\n[1] ensure_events_table()...')
    ensure_events_table(db)
    print('    ✓ Table ensured')

    # 2. emit_event — started
    print('\n[2] emit_event(phase=scout, event_type=started)...')
    eid1 = emit_event(db, test_mission, phase='scout', event_type='started',
                      data={'query_count': 200})
    print(f'    ✓ Event id={eid1}')

    # 3. emit_event — progress with quality metrics
    print('\n[3] emit_event(phase=harvest, event_type=progress, quality=...)...')
    eid2 = emit_event(db, test_mission, phase='harvest', event_type='progress',
                      data={'urls_harvested': 42},
                      coverage=0.65, depth=0.55, confidence=0.70)
    print(f'    ✓ Event id={eid2}')

    # 4. emit_event — completed
    print('\n[4] emit_event(phase=process, event_type=completed)...')
    eid3 = emit_event(db, test_mission, phase='process', event_type='completed',
                      data={'docs_processed': 38},
                      coverage=0.80, depth=0.72, confidence=0.85)
    print(f'    ✓ Event id={eid3}')

    # 5. emit_event — done
    print('\n[5] emit_event(phase=done, event_type=done)...')
    eid4 = emit_event(db, test_mission, phase='done', event_type='done',
                      data={'message': 'mission complete'})
    print(f'    ✓ Event id={eid4}')

    # 6. get_events — all
    print('\n[6] get_events(since_id=0)...')
    evs = get_events(db, test_mission, since_id=0)
    print(f'    ✓ Retrieved {len(evs)} events')
    for ev in evs:
        print(f'      id={ev["id"]} phase={ev["phase"]} type={ev["event_type"]} '
              f'data={ev["data"]}')

    # 7. get_events — pagination (since_id = first event)
    print(f'\n[7] get_events(since_id={eid1})...')
    evs_page = get_events(db, test_mission, since_id=eid1)
    print(f'    ✓ Retrieved {len(evs_page)} events after id={eid1}')

    # 8. get_latest_quality
    print('\n[8] get_latest_quality()...')
    quality = get_latest_quality(db, test_mission)
    print(f'    ✓ quality={quality}')

    # 9. sse_stream — quick test (should return after hitting 'done' event)
    print('\n[9] sse_stream() — consuming until done...')
    count = 0
    for chunk in sse_stream(db, test_mission, poll_interval=0.1):
        count += 1
        line = chunk.strip()
        print(f'    SSE chunk {count}: {line[:100]}')
        if count >= 10:
            print('    (capped at 10 chunks for test)')
            break
    print(f'    ✓ SSE stream produced {count} chunk(s)')

    # 10. FastAPI availability
    print(f'\n[10] FastAPI SSE router: {"✓ available" if _FASTAPI_AVAILABLE else "✗ not available"}')

    print('\n[hive2_events] ✅  All tests passed\n')
