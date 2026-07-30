"""BeHive CLI — research from your terminal, serve for AI assistants."""

import argparse
import asyncio
import os
import sys


def _install_compat():
    """Install legacy import shims before any engine imports."""
    try:
        from behive.compat.shims import install_shims, install_ops_shim
        install_ops_shim()  # Must be first — registers meta_path finder
        install_shims()
    except ImportError:
        pass


def main():
    _install_compat()
    parser = argparse.ArgumentParser(
        prog="behive",
        description="BeHive — Deep Research Engine. Structured knowledge, not text soup.",
    )
    sub = parser.add_subparsers(dest="command")

    # ─── serve ────────────────────────────────────────────────────────────
    serve_p = sub.add_parser("serve", help="Start BeHive API + MCP server")
    serve_p.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve_p.add_argument("--port", type=int, default=8091, help="API port (default: 8091)")
    serve_p.add_argument("--mcp-port", type=int, default=8090, help="MCP port (default: 8090)")
    serve_p.add_argument("--workers", type=int, default=1, help="Uvicorn workers")
    serve_p.add_argument("--reload", action="store_true", help="Auto-reload on file changes")

    # ─── research ─────────────────────────────────────────────────────────
    research_p = sub.add_parser("research", help="Run a research mission")
    research_p.add_argument("topic", help="Research question or topic")
    research_p.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4, 5])
    research_p.add_argument("--scale", type=int, default=None, help="Task count (30-300)")
    research_p.add_argument("--json", action="store_true", help="Output JSON instead of text")
    research_p.add_argument("--api", default=None, help="Remote API URL (default: local DB)")

    # ─── status ───────────────────────────────────────────────────────────
    status_p = sub.add_parser("status", help="Check server status")
    status_p.add_argument("--api", default="http://127.0.0.1:8091", help="API URL")

    # ─── config ────────────────────────────────────────────────────────────
    config_p = sub.add_parser("config", help="Configure model routing per pipeline stage")
    config_mode = config_p.add_mutually_exclusive_group()
    config_mode.add_argument("--quick", action="store_true", help="Quick setup: one model for all stages")
    config_mode.add_argument("--full", action="store_true", help="Full setup: pick model per stage")
    config_mode.add_argument("--show", action="store_true", help="Show current configuration")
    config_mode.add_argument("--preset", choices=["budget", "balanced", "quality", "local"], help="Apply a named preset")
    config_mode.add_argument("--list-models", action="store_true", help="Show available model presets")
    config_p.add_argument("--stage", choices=["scout", "harvest", "process", "synth"], help="Set model for single stage")
    config_p.add_argument("--model", help="Model name or litellm string (use with --stage)")

    # ─── version ──────────────────────────────────────────────────────────
    sub.add_parser("version", help="Show version")

    # ─── db ────────────────────────────────────────────────────────────────
    db_p = sub.add_parser("db", help="Database management")
    db_sub = db_p.add_subparsers(dest="db_action")
    db_sub.add_parser("init", help="Initialize database schema")

    # ─── missions ──────────────────────────────────────────────────────────
    missions_p = sub.add_parser("missions", help="List recent research missions")
    missions_p.add_argument("--limit", type=int, default=10, help="Number of missions to show")

    args = parser.parse_args()

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "research":
        asyncio.run(cmd_research(args))
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "db":
        cmd_db(args)
    elif args.command == "missions":
        cmd_missions(args)
    elif args.command == "version":
        from behive import __version__
        print(f"behive {__version__}")
    else:
        parser.print_help()


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — model routing configuration
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_config(args):
    """Configure model routing per pipeline stage."""
    from behive.config import (
        run_quick_config, run_full_config, show_config,
        load_config, save_config, RECOMMENDED_COMBOS,
        MODEL_PRESETS, STAGES, CONFIG_FILE,
    )

    if args.preset:
        # Non-interactive preset application
        combo = RECOMMENDED_COMBOS[args.preset]
        cfg = load_config()
        cfg["models"] = {stage: combo[stage] for stage in STAGES}
        save_config(cfg)
        print(f"\n✅ Applied '{args.preset}' preset: {combo['description']}")
        for stage in STAGES:
            resolved = MODEL_PRESETS.get(combo[stage], combo[stage])
            print(f"   {stage:10s} → {resolved}")
        print(f"\n   Config: {CONFIG_FILE}\n")
    elif getattr(args, 'list_models', False):
        # Show all available presets
        print("\n🐝 BeHive Model Presets\n")
        print(f"  {'Preset':<20} {'Model String'}")
        print(f"  {'─'*20} {'─'*50}")
        for name, model in MODEL_PRESETS.items():
            print(f"  {name:<20} {model}")
        print(f"\n  💡 You can also use ANY litellm-compatible model string directly:")
        print(f"     BEHIVE_MODEL=anthropic/claude-sonnet-5-20260801")
        print(f"     BEHIVE_MODEL=openai/gpt-5.6-turbo")
        print(f"     BEHIVE_MODEL=ollama/mixtral:8x22b")
        print(f"     behive config --stage synth --model deepseek/deepseek-r1\n")
    elif args.stage and args.model:
        # Set single stage model
        cfg = load_config()
        if "models" not in cfg:
            cfg["models"] = {}
        cfg["models"][args.stage] = args.model
        save_config(cfg)
        resolved = MODEL_PRESETS.get(args.model, args.model)
        print(f"\n✅ {args.stage} → {resolved}")
        print(f"   Config: {CONFIG_FILE}\n")
    elif args.quick:
        run_quick_config()
    elif args.full:
        run_full_config()
    else:
        show_config()


# ═══════════════════════════════════════════════════════════════════════════════
# SERVE — starts both API + MCP server
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_serve(args):
    """Start BeHive API server + MCP endpoint."""
    import multiprocessing

    # Check required env vars
    _check_env()

    print(f"""
\033[33m  ╔══════════════════════════════════════╗
  ║   🐝 BeHive Research Engine v0.3.1   ║
  ╚══════════════════════════════════════╝\033[0m

  API:  http://{args.host}:{args.port}
  MCP:  http://{args.host}:{args.mcp_port}/mcp
  Docs: http://{args.host}:{args.port}/docs

  \033[2mPress Ctrl+C to stop\033[0m
""")

    # Start MCP in a subprocess
    mcp_proc = multiprocessing.Process(
        target=_run_mcp_server,
        args=(args.host, args.mcp_port),
        daemon=True,
    )
    mcp_proc.start()

    # Start API in main process (uvicorn)
    try:
        import uvicorn
        uvicorn.run(
            "behive.server:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            reload=args.reload,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n\033[33m🐝 BeHive stopped.\033[0m")
    finally:
        mcp_proc.terminate()


def _run_mcp_server(host: str, port: int):
    """Run MCP server in subprocess."""
    import uvicorn
    uvicorn.run(
        "behive.mcp_server:app",
        host=host,
        port=port,
        log_level="warning",
    )


def _check_env():
    """Check that necessary environment variables are set."""
    # At minimum need an LLM key
    has_key = any(os.environ.get(k) for k in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",  # Bedrock
    ])
    if not has_key:
        print("\033[31m⚠ No LLM API key found.\033[0m")
        print("  Set one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, or AWS credentials")
        print("  Example: export ANTHROPIC_API_KEY=sk-...")
        print()
        print()

    # Check DB
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("BEHIVE_DB_URL")
    if not db_url:
        print("\033[33mℹ No DATABASE_URL set — using default postgresql://localhost:5432/hive\033[0m")
        print("  To configure: export DATABASE_URL=postgresql://user:pass@host:5432/db")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
# RESEARCH — run a mission from CLI
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_research(args):
    """Run a research mission and print results."""
    from behive import research, BeHiveClient

    print(f"\033[33m🐝 Researching: {args.topic}\033[0m")
    print(f"   Depth: {args.depth} | Scale: {args.scale or 'auto'}")
    print()

    if args.api:
        client = BeHiveClient(api_url=args.api)
    else:
        client = BeHiveClient()

    result = await client.research(args.topic, depth=args.depth, scale=args.scale)

    if args.json:
        import json
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_result(result)


def _print_result(result):
    """Pretty-print a research result."""
    from behive.models import ResearchResult

    if result.error:
        print(f"\033[31m✗ Error: {result.error}\033[0m")
        return

    print(f"\033[32m✓ Done\033[0m — {len(result.claims)} claims | "
          f"avg quality: {result.avg_quality:.3f} | "
          f"{result.duration_seconds:.0f}s")
    print()

    # Top claims
    top = sorted(result.claims, key=lambda c: c.quality_score, reverse=True)[:10]
    print("\033[33m─── Top Claims ───\033[0m")
    for c in top:
        score_color = "\033[32m" if c.quality_score >= 0.8 else "\033[33m"
        print(f"  {score_color}[{c.quality_score:.2f}]\033[0m {c.text[:120]}")
    print()

    # Entities
    if result.entities:
        print(f"\033[33m─── Entities ({len(result.entities)}) ───\033[0m")
        for e in result.entities[:15]:
            print(f"  • {e.name} ({e.entity_type})")
        print()

    # Report excerpt
    if result.report:
        lines = result.report.strip().split("\n")[:10]
        print("\033[33m─── Report (excerpt) ───\033[0m")
        for line in lines:
            print(f"  {line}")
        print("  ...")


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_status(args):
    """Check BeHive server health."""
    import httpx
    try:
        resp = httpx.get(f"{args.api}/health", timeout=5)
        data = resp.json()
        print(f"\033[32m✓ BeHive is running\033[0m")
        print(f"  Version:   {data.get('version', '?')}")
        print(f"  Missions:  {data.get('missions_total', '?')}")
        print(f"  Claims:    {data.get('claims_total', '?')}")
        print(f"  Avg Qual:  {data.get('avg_quality', '?')}")
        print(f"  DB:        {data.get('db', '?')}")
    except Exception as e:
        print(f"\033[31m✗ Cannot reach BeHive at {args.api}\033[0m")
        print(f"  Error: {e}")
        print(f"  Run: behive serve")


# ═══════════════════════════════════════════════════════════════════════════════
# DB — database management
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_db(args):
    """Database management commands."""
    if args.db_action == "init":
        _db_init()
    else:
        print("Usage: behive db init")
        print("  Initialize the BeHive database schema.")


def _db_init():
    """Create all tables in PostgreSQL from bundled schema."""
    import importlib.resources
    import psycopg2

    # Find init-db.sql bundled with the package
    sql_path = None
    try:
        # Python 3.9+
        ref = importlib.resources.files("behive").joinpath("init-db.sql")
        sql_content = ref.read_text(encoding="utf-8")
    except (AttributeError, FileNotFoundError):
        # Fallback: look relative to this file
        this_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(this_dir, "init-db.sql")
        if not os.path.exists(candidate):
            # Try docker/ directory (development)
            candidate = os.path.join(this_dir, "..", "..", "docker", "init-db.sql")
        if os.path.exists(candidate):
            with open(candidate) as f:
                sql_content = f.read()
        else:
            print("\033[31m✗ Cannot find init-db.sql\033[0m")
            print("  Expected at: src/behive/init-db.sql or docker/init-db.sql")
            sys.exit(1)

    # Connect to database
    db_url = os.environ.get("BEHIVE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        user = os.environ.get("HIVE_PG_USER", "behive")
        password = os.environ.get("HIVE_PG_PASSWORD", "behive")
        host = os.environ.get("HIVE_PG_HOST", "localhost")
        port = os.environ.get("HIVE_PG_PORT", "5432")
        db = os.environ.get("HIVE_PG_DATABASE", "behive")
        db_url = f"postgresql://{user}:***@{host}:{port}/{db}"

    print(f"  Connecting to: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql_content)
        # Count tables created
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """)
        table_count = cur.fetchone()[0]
        conn.close()
        print(f"\033[32m✓ Database initialized — {table_count} tables ready\033[0m")
    except psycopg2.OperationalError as e:
        print(f"\033[31m✗ Cannot connect to database\033[0m")
        print(f"  {e}")
        print(f"\n  Set BEHIVE_DB_URL or HIVE_PG_* environment variables.")
        print(f"  Example: export BEHIVE_DB_URL=postgresql://user:***@localhost:5432/behive")
        sys.exit(1)
    except Exception as e:
        print(f"\033[31m✗ Schema error: {e}\033[0m")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# MISSIONS — list recent missions
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_missions(args):
    """List recent research missions from the database."""
    import psycopg2

    db_url = os.environ.get("BEHIVE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        user = os.environ.get("HIVE_PG_USER", "behive")
        password = os.environ.get("HIVE_PG_PASSWORD", "behive")
        host = os.environ.get("HIVE_PG_HOST", "localhost")
        port = os.environ.get("HIVE_PG_PORT", "5432")
        db = os.environ.get("HIVE_PG_DATABASE", "behive")
        db_url = f"postgresql://{user}:***@{host}:{port}/{db}"

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.topic, m.status, m.phase, m.created_at,
                   COALESCE(c.cnt, 0), COALESCE(c.avg_q, 0)
            FROM hive_missions m
            LEFT JOIN (
                SELECT mission_id, COUNT(*) as cnt, AVG(quality_score) as avg_q
                FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
                GROUP BY mission_id
            ) c ON c.mission_id = m.id
            ORDER BY m.created_at DESC
            LIMIT %s
        """, (args.limit,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            print("  No missions found. Run: behive research \"your topic\"")
            return

        print(f"\033[33m🐝 Recent Missions ({len(rows)})\033[0m\n")
        print(f"  {'ID':<28} {'Status':<8} {'Claims':>6} {'Quality':>8}  Topic")
        print(f"  {'─'*28} {'─'*8} {'─'*6} {'─'*8}  {'─'*30}")
        for row in rows:
            mid, topic, status, phase, created, claims, avg_q = row
            status_icon = {"done": "✓", "running": "⟳", "error": "✗", "queued": "◌"}.get(status, "?")
            topic_short = (topic or "")[:40]
            print(f"  {mid:<28} {status_icon} {status:<6} {claims:>6} {avg_q:>7.3f}  {topic_short}")
    except Exception as e:
        print(f"\033[31m✗ Cannot connect to database: {e}\033[0m")
        print(f"  Set BEHIVE_DB_URL or run: behive db init")


if __name__ == "__main__":
    main()
