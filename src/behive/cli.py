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
    research_p.add_argument("--no-db", action="store_true",
                           help="Run with DuckDB (zero setup, results saved locally in ~/.behive/)")

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

    # ─── doctor ──────────────────────────────────────────────────────────
    sub.add_parser("doctor", help="Check system health (DB, LLM, browser, dependencies)")

    # ─── quickstart ──────────────────────────────────────────────────────
    sub.add_parser("quickstart", help="Interactive first-time setup (Docker or manual)")

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
    elif args.command == "doctor":
        cmd_doctor()
    elif args.command == "quickstart":
        cmd_quickstart()
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
    from behive import __version__

    # Check required env vars
    _check_env()

    ver = __version__
    print(f"""
\033[33m  ╔══════════════════════════════════════╗
  ║   🐝 BeHive Research Engine v{ver:<6}║
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
    """Check that necessary environment variables are set. Exit with helpful message if critical deps missing."""
    issues = []

    # Check LLM key
    has_key = any(os.environ.get(k) for k in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "AWS_ACCESS_KEY_ID",  # Bedrock
        "BEHIVE_MODEL",  # litellm model string (implies key is set)
    ])
    if not has_key:
        issues.append(("LLM", "No LLM API key found. Set one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, or AWS credentials."))

    # Check DB connectivity
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("BEHIVE_DB_URL")
    has_pg_vars = os.environ.get("HIVE_PG_USER") or os.environ.get("HIVE_PG_PASSWORD")
    if not db_url and not has_pg_vars:
        issues.append(("DB", "No database configured. Set BEHIVE_DB_URL or HIVE_PG_* environment variables."))
    else:
        # Test connection
        try:
            import psycopg2
            from behive.server import get_db_url
            conn = psycopg2.connect(get_db_url())
            conn.close()
        except Exception as e:
            issues.append(("DB", f"Cannot connect to database: {e}"))

    if issues:
        print("\033[33m")
        print("  ╔══════════════════════════════════════╗")
        print("  ║   🐝 BeHive — Setup Required        ║")
        print("  ╚══════════════════════════════════════╝\033[0m")
        print()
        for category, msg in issues:
            print(f"  \033[31m✗ [{category}]\033[0m {msg}")
        print()
        print("  Quick setup:")
        print("    1. Start PostgreSQL and create a database:")
        print("       createdb behive")
        print("    2. Set environment variables:")
        print("       export BEHIVE_DB_URL=postgresql://user:pass@localhost:5432/behive")
        print("       export OPENAI_API_KEY=sk-...")
        print("    3. Initialize the schema:")
        print("       behive db init")
        print("    4. Start the server:")
        print("       behive serve")
        print()
        if any(cat == "DB" for cat, _ in issues):
            sys.exit(1)
        print()


# ═══════════════════════════════════════════════════════════════════════════════
# RESEARCH — run a mission from CLI
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_research(args):
    """Run a research mission and print results."""
    import os
    from behive import research, BeHiveClient

    print(f"\033[33m🐝 Researching: {args.topic}\033[0m")
    print(f"   Depth: {args.depth} | Scale: {args.scale or 'auto'}")
    if getattr(args, 'no_db', False):
        print(f"   Mode: \033[36mDuckDB\033[0m (local storage in ~/.behive/research.duckdb)")
    print()

    if getattr(args, 'no_db', False):
        # DuckDB mode: set backend env and use normal pipeline
        os.environ["BEHIVE_BACKEND"] = "duckdb"
        # Force re-evaluation of backend
        import behive.engine.db as _db_mod
        _db_mod._backend_override = "duckdb"
        
        # Try local pipeline directly
        try:
            from behive.engine.db_duckdb import is_available
            if not is_available():
                print("\033[31m✗ DuckDB not installed. Run: pip install duckdb\033[0m")
                return
        except ImportError:
            print("\033[31m✗ DuckDB not available. Run: pip install 'behive[duckdb]'\033[0m")
            return

    if args.api:
        client = BeHiveClient(api_url=args.api)
    else:
        try:
            client = BeHiveClient()
        except RuntimeError as e:
            # Offer --no-db as fallback
            print(f"\033[31m✗ {e}\033[0m")
            print()
            print("\033[33mTip:\033[0m Try \033[1mbehive research '{}' --no-db\033[0m to run without database.".format(args.topic))
            return

    result = await client.research(args.topic, depth=args.depth, scale=args.scale)

    if args.json:
        import json
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_result(result)


async def _research_no_db(args):
    """Run research in no-DB mode — web search + LLM extraction, results to stdout only."""
    import time
    import json as json_mod

    topic = args.topic
    depth = args.depth

    # Scale down for no-db mode (no persistence, so keep it focused)
    scale_map = {1: 5, 2: 10, 3: 15, 4: 20, 5: 30}
    n_sources = args.scale or scale_map.get(depth, 15)

    start = time.time()

    # Step 1: Scout — find sources via web search
    print("\033[36m  ⟳ Scouting sources...\033[0m")
    try:
        sources = await _nodb_scout(topic, n_sources)
    except Exception as e:
        print(f"\033[31m  ✗ Scout failed: {e}\033[0m")
        print(f"    Make sure you have a web search backend configured.")
        print(f"    Chromium: pip install playwright && playwright install chromium")
        return

    if not sources:
        print("\033[31m  ✗ No sources found. Check your internet connection.\033[0m")
        return

    print(f"\033[32m  ✓\033[0m Found {len(sources)} sources")

    # Step 2: Harvest — fetch content
    print("\033[36m  ⟳ Harvesting content...\033[0m")
    pages = await _nodb_harvest(sources)
    print(f"\033[32m  ✓\033[0m Harvested {len(pages)} pages ({sum(len(p.get('text','')) for p in pages)//1000}KB)")

    # Step 3: Extract claims via LLM
    print("\033[36m  ⟳ Extracting claims...\033[0m")
    try:
        claims = await _nodb_extract(topic, pages)
    except Exception as e:
        print(f"\033[31m  ✗ LLM extraction failed: {e}\033[0m")
        print(f"    Set OPENAI_API_KEY or ANTHROPIC_API_KEY")
        return

    duration = time.time() - start
    print(f"\033[32m  ✓\033[0m Extracted {len(claims)} claims in {duration:.1f}s")
    print()

    # Output
    if getattr(args, 'json', False):
        output = {
            "topic": topic,
            "claims": claims,
            "sources": sources[:10],
            "duration_seconds": round(duration, 1),
            "mode": "no-db",
        }
        print(json_mod.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"\033[33m─── Results: {topic} ───\033[0m")
        print(f"    {len(claims)} claims | {len(sources)} sources | {duration:.0f}s")
        print()
        for i, claim in enumerate(claims[:20], 1):
            conf = claim.get("confidence", 0)
            color = "\033[32m" if conf >= 0.8 else "\033[33m" if conf >= 0.6 else "\033[0m"
            print(f"  {color}[{conf:.2f}]\033[0m {claim['text'][:120]}")
            if claim.get("source"):
                print(f"         \033[90m↳ {claim['source'][:80]}\033[0m")

        print()
        print(f"\033[90m  Mode: no-db (not saved). For persistence: behive serve + behive research '{topic}'\033[0m")


async def _nodb_scout(topic: str, n: int) -> list[str]:
    """Simple web search for sources."""
    import asyncio
    try:
        from behive.engine.search_backends import search
        results = await search(topic, max_results=n)
        return [r["url"] for r in results if r.get("url")]
    except Exception:
        # Fallback: basic DuckDuckGo via aiohttp
        import aiohttp
        import json
        url = f"https://html.duckduckgo.com/html/?q={topic.replace(' ', '+')}"
        return []  # DDG HTML parsing too fragile for fallback


async def _nodb_harvest(urls: list[str]) -> list[dict]:
    """Fetch page content without saving to DB."""
    import asyncio
    import aiohttp

    pages = []
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [_fetch_page(session, url) for url in urls[:20]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and r.get("text"):
                pages.append(r)

    return pages


async def _fetch_page(session, url: str) -> dict:
    """Fetch a single page."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BeHive/0.4; +https://behive.site)"}
        async with session.get(url, headers=headers, ssl=False) as resp:
            if resp.status == 200:
                text = await resp.text()
                # Simple text extraction (strip HTML tags)
                import re
                clean = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
                clean = re.sub(r'<[^>]+>', ' ', clean)
                clean = re.sub(r'\s+', ' ', clean).strip()
                return {"url": url, "text": clean[:8000]}
    except Exception:
        pass
    return {}


async def _nodb_extract(topic: str, pages: list[dict]) -> list[dict]:
    """Extract claims from pages using LLM (no DB needed)."""
    import asyncio
    from behive.engine.llm import complete

    # Combine page texts
    combined = "\n\n---\n\n".join(
        f"Source: {p['url']}\n{p['text'][:3000]}" for p in pages[:10]
    )

    prompt = f"""You are a research analyst. Extract factual claims from the sources below about: {topic}

Return a JSON array of claims. Each claim should have:
- "text": the factual statement (specific, verifiable, with numbers when available)
- "confidence": 0.0-1.0 how well supported by sources
- "source": source URL

Focus on specific facts, statistics, dates, and concrete information. Ignore opinions and vague statements.
Return ONLY the JSON array, no other text.

SOURCES:
{combined[:12000]}"""

    response = await asyncio.to_thread(complete, prompt, max_tokens=4000)

    # Parse JSON from response
    import json
    import re

    # Extract JSON array from response
    text = response if isinstance(response, str) else str(response)
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            claims = json.loads(match.group())
            return claims if isinstance(claims, list) else []
        except json.JSONDecodeError:
            pass
    return []


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
# DOCTOR — system health check
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_doctor():
    """Check all BeHive dependencies and report status."""
    from behive import __version__
    print(f"\n  🐝 BeHive Doctor v{__version__}\n")
    checks = []

    # 1. Python version
    import platform
    py_ver = platform.python_version()
    ok = tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 10)
    checks.append(("Python", f"{py_ver}", ok))

    # 2. PostgreSQL connection
    try:
        import psycopg2
        from behive.server import get_db_url
        db_url = get_db_url()
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
        tables = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        missions = cur.fetchone()[0]
        conn.close()
        checks.append(("Database", f"connected ({tables} tables, {missions} missions)", True))
    except Exception as e:
        checks.append(("Database", str(e)[:80], False))

    # 3. LLM connectivity
    llm_keys = {
        "OPENAI_API_KEY": "OpenAI",
        "ANTHROPIC_API_KEY": "Anthropic",
        "GROQ_API_KEY": "Groq",
        "MISTRAL_API_KEY": "Mistral",
        "AWS_ACCESS_KEY_ID": "Bedrock",
    }
    found_keys = [name for env, name in llm_keys.items() if os.environ.get(env)]
    behive_model = os.environ.get("BEHIVE_MODEL", "")
    if found_keys or behive_model:
        detail = ", ".join(found_keys) if found_keys else f"BEHIVE_MODEL={behive_model}"
        checks.append(("LLM", f"configured ({detail})", True))
    else:
        checks.append(("LLM", "no API key found", False))

    # 4. Playwright/Chromium (for web search)
    try:
        import subprocess
        result = subprocess.run(
            ["python3", "-c", "from playwright.sync_api import sync_playwright; print('ok')"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            checks.append(("Browser", "Playwright + Chromium available", True))
        else:
            checks.append(("Browser", "Playwright installed but Chromium missing (run: playwright install chromium)", False))
    except FileNotFoundError:
        checks.append(("Browser", "not installed (pip install playwright && playwright install chromium)", False))
    except Exception as e:
        checks.append(("Browser", f"check failed: {e}", False))

    # 5. Optional: Neo4j
    neo4j_uri = os.environ.get("BEHIVE_NEO4J_URI")
    if neo4j_uri:
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(neo4j_uri, auth=("neo4j", os.environ.get("BEHIVE_NEO4J_PASSWORD", "")))
            driver.verify_connectivity()
            driver.close()
            checks.append(("Neo4j", f"connected ({neo4j_uri})", True))
        except Exception as e:
            checks.append(("Neo4j", f"configured but unreachable: {e}", False))
    else:
        checks.append(("Neo4j", "not configured (optional — for knowledge graphs)", None))

    # 6. Optional: Qdrant
    qdrant_url = os.environ.get("BEHIVE_QDRANT_URL")
    if qdrant_url:
        checks.append(("Qdrant", f"configured ({qdrant_url})", True))
    else:
        checks.append(("Qdrant", "not configured (optional — for vector search)", None))

    # Print results
    for name, detail, ok in checks:
        if ok is True:
            icon = "\033[32m✓\033[0m"
        elif ok is False:
            icon = "\033[31m✗\033[0m"
        else:
            icon = "\033[2m○\033[0m"
        print(f"  {icon} {name:<12} {detail}")

    # Summary
    critical = [(n, d) for n, d, ok in checks if ok is False]
    if critical:
        print(f"\n  \033[31m{len(critical)} issue(s) found.\033[0m Fix them and run `behive doctor` again.\n")
        sys.exit(1)
    else:
        print(f"\n  \033[32mAll checks passed.\033[0m BeHive is ready.\n")


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


# ═══════════════════════════════════════════════════════════════════════════════
# QUICKSTART — Interactive first-time setup
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_quickstart():
    """Interactive first-time setup for BeHive."""
    import shutil
    from behive import __version__

    print(f"\033[33m🐝 BeHive {__version__} — Quick Setup\033[0m")
    print("=" * 50)
    print()

    # Step 1: Check LLM key
    llm_key = _detect_llm_key()
    if llm_key:
        print(f"  \033[32m✓\033[0m LLM key detected ({llm_key})")
    else:
        print("  \033[31m✗\033[0m No LLM key found")
        print()
        print("  Set one of these environment variables:")
        print("    export OPENAI_API_KEY=sk-...")
        print("    export ANTHROPIC_API_KEY=sk-ant-...")
        print("    export GROQ_API_KEY=gsk_...")
        print()
        print("  Then run \033[33mbehive quickstart\033[0m again.")
        return

    # Step 2: Check Docker
    has_docker = shutil.which("docker") is not None
    has_compose = shutil.which("docker") is not None  # docker compose is subcommand now

    print()
    if has_docker:
        print(f"  \033[32m✓\033[0m Docker found")
        print()
        print("  \033[1mOption A: Docker (recommended, 30 seconds)\033[0m")
        print("  ┌─────────────────────────────────────────────┐")
        print("  │  git clone https://github.com/qa10devteam/behive")
        print("  │  cd behive")
        print("  │  cp .env.example .env   # add your API key")
        print("  │  docker compose up -d")
        print("  │")
        print("  │  # Ready! Test:")
        print("  │  behive research 'your topic' --api http://localhost:8091")
        print("  └─────────────────────────────────────────────┘")
    else:
        print(f"  ○ Docker not found (optional)")

    # Step 3: Quick mode (DuckDB)
    print()
    print("  \033[1mOption B: Local research (DuckDB, zero setup)\033[0m")
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  behive research 'NVIDIA GPU market' --no-db")
    print("  │")
    print("  │  Uses DuckDB (~/.behive/research.duckdb).")
    print("  │  Results are saved locally. No PostgreSQL.")
    print("  │  Perfect for solo research and prototyping.")
    print("  └─────────────────────────────────────────────┘")

    # Step 4: Full setup
    print()
    print("  \033[1mOption C: Full self-hosted (PostgreSQL)\033[0m")
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  sudo apt install postgresql")
    print("  │  behive db init")
    print("  │  behive serve")
    print("  │")
    print("  │  # In another terminal:")
    print("  │  behive research 'your topic'")
    print("  └─────────────────────────────────────────────┘")

    # MCP config
    print()
    print("  \033[33m─── MCP Integration (Claude/Cursor/Windsurf) ───\033[0m")
    print("""  Add to your MCP config:
  {
    "mcpServers": {
      "behive": {
        "url": "http://localhost:8090/mcp",
        "transport": "streamable-http"
      }
    }
  }""")
    print()
    print("  Docs: https://behive.site")
    print()


def _detect_llm_key() -> str:
    """Check for available LLM API keys."""
    import os
    import shutil
    for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
                 "MISTRAL_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY"]:
        if os.environ.get(name):
            return name
    # Check for Ollama
    if shutil.which("ollama"):
        return "ollama (local)"
    return ""
