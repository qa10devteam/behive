# BeHive — Plan Realizacji Eliminacji Długu Technologicznego
## Wersja: 1.0 | Data: 2026-07-31 | Autor: Hermes Agent
## Cel: Od 31,673 LOC / 34% coverage → ~28,000 LOC / 50%+ coverage / 0 BYOK violations

---

## ZASADY REALIZACJI

1. **Każda faza kończy się**: `pytest` zielone + `git commit` + `git push`
2. **Pre-commit gate**: `grep -r 'password\|secret\|sk-ant\|ghp_' src/ --include='*.py'` = 0 hits
3. **Nie łamać runtime**: Po każdej fazie `behive serve` musi startować bez erroru
4. **Coverage nie może spaść**: Każda faza utrzymuje lub podnosi % coverage
5. **Commit messages**: Professional English (public repo, 100+ stars)

---

## FAZA 0: QUICK WINS (Dead Code) — est. 30 min

### 0.1 Usunięcie runner.py
- [ ] `rm src/behive/engine/runner.py`
- [ ] Usunąć z `__all__` w `engine/__init__.py` (jeśli jest)
- [ ] Usunąć testy runner z `test_mass_coverage_35.py` (TestRunnerModule)
- [ ] Verify: `grep -rn 'runner' src/behive/ | grep -v __pycache__` = 0

### 0.2 Usunięcie bedrock_compat.py
- [ ] `rm src/behive/engine/bedrock_compat.py`
- [ ] Usunąć test z `test_mass_coverage_35.py` (bedrock_compat import)
- [ ] Verify: `grep -rn 'bedrock_compat' src/behive/ tests/` = 0

### 0.3 Usunięcie quality.py (duplikat content_quality.py)
- [ ] Sprawdzić kto importuje quality: `grep -rn 'from.*quality import\|import.*quality' src/`
- [ ] Update shim: `"hive2_content_quality" → "behive.engine.content_quality"` (already correct)
- [ ] `rm src/behive/engine/quality.py`
- [ ] Usunąć z `__all__` w `engine/__init__.py`
- [ ] Verify: all tests pass

### 0.4 Fix version inconsistency
- [ ] `__init__.py` fallback: `"0.3.3"` → `"0.4.0"`

### 0.5 Clean SHIM_MAP
- [ ] Usunąć entries dla nieistniejących modułów: `hive2_cluster`, `hive2_scout_targeted`, `hive2_watcher`

**Zysk Fazy 0**: -440 LOC, 0 risk, instant coverage bump (~1-2%)

---

## FAZA 1: DEDUPLIKACJA QueenPlanner — est. 1.5h

### 1.1 Analiza — która wersja jest kanoniczna?
- [ ] Porównać trzy kopie (orchestrator.py:206, queen.py:22, queen_planner_extracted.py:21)
- [ ] Zidentyfikować która ma najnowsze poprawki (git blame/log)
- [ ] Decyzja: prawdopodobnie `orchestrator.py` (importowana przez resztę pipeline)

### 1.2 Konsolidacja
- [ ] Wybrać kanoniczną lokalizację: `behive/engine/queen_planner.py` (nowy plik, rename)
- [ ] Move pełny `QueenPlanner` tam z najnowszymi fixami
- [ ] W `orchestrator.py`: `from behive.engine.queen_planner import QueenPlanner`
- [ ] Usunąć `queen.py` i `queen_planner_extracted.py`
- [ ] Update shim: dodać `"hive2_queen_planner": "behive.engine.queen_planner"`

### 1.3 Weryfikacja
- [ ] `python3 -c "from behive.engine.orchestrator import QueenPlanner; print('OK')"`
- [ ] `python3 -c "from behive.engine.queen_planner import QueenPlanner; print('OK')"`
- [ ] Full test suite pass
- [ ] `behive serve` starts

**Zysk Fazy 1**: -1,600 LOC (dwie kopie QueenPlanner usunięte)

---

## FAZA 2: USUNIĘCIE DuckDB — est. 3h

### 2.1 Mapowanie zależności DuckDB
Pliki z DuckDB references (sorted by count):
```
prescout.py         12 refs
orchestrator.py     10 refs
queen_primer.py      9 refs
domain_reputation.py 6 refs
synth.py             5 refs
bionic_quorum.py     5 refs
queen_tools.py       3 refs
scout.py             2 refs
process.py           2 refs
rag.py               1 ref (+ 20 in DuckDB VSS fallback section)
quality.py           1 ref (usunięte w Fazie 0)
harvest.py           1 ref
events.py            1 ref
db_helpers.py        1 ref
content_quality.py   1 ref
db.py                1 ref
```

### 2.2 Kategorie kodu DuckDB do usunięcia
**A. Fallback paths** (rag.py): DuckDB VSS as fallback for Qdrant
- Usunąć cały fallback path (log.info "trying DuckDB VSS fallback")
- Qdrant is primary and only vector store
- Usunąć `DUCKDB_PATH`, `_duckdb_connect` helper

**B. Direct queries** (bionic_quorum.py): `_duckdb_connect()` + SQL queries
- Przepisać na PostgreSQL via `db_helpers.pg_query()`
- 5 callsites: quorum threshold checks

**C. Comments/docstrings** (prescout.py, orchestrator.py, queen_primer.py):
- References do "DuckDB" w komentarzach opisujących architekturę
- Replace z "PostgreSQL"

**D. Connection helpers** (db_helpers.py, db.py):
- Usunąć DuckDB connect/retry logic
- Zostawić TYLKO PostgreSQL pool

### 2.3 Execution order
1. [ ] `rag.py` — usunąć DuckDB VSS fallback section (~100 LOC)
2. [ ] `bionic_quorum.py` — rewrite `_duckdb_connect()` → PG queries
3. [ ] `prescout.py` — replace 12 DuckDB refs (probably comments + dead paths)
4. [ ] `orchestrator.py` — replace 10 refs
5. [ ] `queen_primer.py` — replace 9 refs
6. [ ] `domain_reputation.py` — replace 6 refs
7. [ ] `synth.py` — replace 5 refs
8. [ ] Remaining files (1-3 refs each) — batch cleanup
9. [ ] `pyproject.toml` — verify duckdb NOT in deps (already confirmed absent)

### 2.4 Weryfikacja
- [ ] `grep -rn 'duckdb\|DuckDB\|_duckdb' src/behive/` = 0 hits
- [ ] Full test suite pass
- [ ] `behive serve` starts
- [ ] `behive research "test query" --depth 1` completes (if DB available)

**Zysk Fazy 2**: -500 to -800 LOC, eliminacja fałszywego fallback path

---

## FAZA 3: BYOK COMPLIANCE (boto3 → llm.py) — est. 4h

### 3.1 Inventory direct boto3 calls
```
rag.py              — _get_bedrock(), _haiku_call(), embed via Bedrock Titan
queen_feedback.py   — direct boto3.client("bedrock-runtime"), MODEL_HAIKU
intel_summary.py    — _bedrock_fallback(), hardcoded model IDs
falsifier.py        — BEDROCK_MODEL, direct invoke_model
queen_fact_mem.py   — _haiku(), direct boto3
graph_engine.py     — boto3 import (for embeddings?)
master_intelligence.py — boto3 import
process.py          — boto3 import
```

### 3.2 Pattern: Replace direct calls with llm.complete()
**Before** (queen_feedback.py):
```python
import boto3
MODEL_HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
bc = boto3.client("bedrock-runtime", region_name="eu-central-1")
body = json.dumps({"anthropic_version":"bedrock-2023-05-31",...})
r = bc.invoke_model(modelId=MODEL_HAIKU, body=body)
```

**After**:
```python
from behive.engine.llm import complete
result = complete(prompt, stage="feedback", max_tokens=600)
```

### 3.3 Special cases
**Embeddings** (`rag.py`):
- Currently uses Bedrock Titan for embeddings
- Replace with `sentence-transformers` local (already in `[qdrant]` extras)
- Or: add `embed()` function to `llm.py` that routes via litellm embedding

**Model selection** (`llm.py` stages):
- `llm.py` already supports stage-based model routing
- Map old hardcoded models to stages:
  - `_haiku_call` → `stage="fast"` (cheap/quick operations)
  - `_bedrock_fallback` → `stage="process"` (standard)
  - Queen planning → `stage="plan"` (premium model)

### 3.4 Execution order
1. [ ] Extend `llm.py` with `embed()` function (litellm.embedding)
2. [ ] `queen_feedback.py` — replace direct boto3 (most isolated)
3. [ ] `queen_fact_mem.py` — replace `_haiku()` function
4. [ ] `intel_summary.py` — replace `_bedrock_fallback()`
5. [ ] `falsifier.py` — replace `BEDROCK_MODEL` + direct calls
6. [ ] `rag.py` — replace `_get_bedrock()`, `_haiku_call()`, Titan embed
7. [ ] `graph_engine.py`, `master_intelligence.py`, `process.py` — remove boto3 import
8. [ ] Remove `constants.py` Bedrock constants (BEDROCK_SERVICE, etc.)
9. [ ] Remove all `import boto3` from engine/ (only `llm.py` may optionally import)

### 3.5 Weryfikacja
- [ ] `grep -rn 'import boto3' src/behive/ | grep -v llm.py` = 0
- [ ] `grep -rn 'invoke_model\|_haiku_call\|_get_bedrock\|_bedrock_fallback' src/behive/` = 0
- [ ] `grep -rn 'eu.anthropic\|us.anthropic' src/behive/ | grep -v '#\|test\|example'` = 0
- [ ] Test: `unset AWS_*; BEHIVE_MODEL=gpt-4o behive serve` — no import error
- [ ] Test: `BEHIVE_MODEL=ollama/llama3 behive serve` — no crash
- [ ] Full test suite pass

**Zysk Fazy 3**: -200 LOC code, +100% BYOK compliance, eliminacja AWS dependency

---

## FAZA 4: LEGACY IMPORTS CLEANUP — est. 1.5h

### 4.1 Replace all hive2_* imports
92 occurrences across engine modules. Pattern:
```python
# Before:
from hive2_queen_memory import QueenMemory
# After:
from behive.engine.queen_memory import QueenMemory
```

### 4.2 Execution
1. [ ] Automated find-replace script:
```bash
find src/behive/engine/ -name '*.py' -exec sed -i   's/from hive2_queen_memory/from behive.engine.queen_memory/g' {} +
# ... repeat for each hive2_* module
```
2. [ ] Verify: `grep -rn 'from hive2_\|import hive2_' src/behive/` = 0
3. [ ] Mark `compat/shims.py` as deprecated (keep for external users who may have `hive2_*` imports)
4. [ ] Add deprecation warning in `install_shims()`

### 4.3 Weryfikacja
- [ ] `python3 -c "from behive.engine.orchestrator import QueenPlanner; print('OK')"`
- [ ] Full test suite pass
- [ ] `behive serve` starts

**Zysk Fazy 4**: Eliminacja runtime shim dependency, cleaner imports

---

## FAZA 5: ARCHITEKTURA GRAPH ENGINE — est. 2h

### 5.1 Decyzja: graph_engine.py vs knowledge_graph.py
- `knowledge_graph.py` (top-level) → Neo4j, 93 LOC, clean
- `engine/graph_engine.py` → DuckDB + NetworkX + HDBSCAN, 1210 LOC, broken (DuckDB dead)

**Opcje**:
A. Usunąć `graph_engine.py` (1210 LOC), zostawić `knowledge_graph.py` (Neo4j)
B. Przepisać `graph_engine.py` na PostgreSQL (duży effort)

**Rekomendacja**: Opcja A — `knowledge_graph.py` + PostgreSQL JSON/array queries
wystarczą do entity/relationship queries. HDBSCAN clustering można dodać
osobno potem (jako nowy moduł na PostgreSQL data).

### 5.2 Execution (jeśli Opcja A)
1. [ ] Audit: kto importuje `graph_engine`?
2. [ ] Redirect callers to `knowledge_graph.py` + PostgreSQL queries
3. [ ] `rm src/behive/engine/graph_engine.py`
4. [ ] Update engine/__init__.py docstring

### 5.3 Weryfikacja
- [ ] Tests pass
- [ ] Neo4j queries still work

**Zysk Fazy 5**: -1,210 LOC lub refactored clean module

---

## FAZA 6: EXCEPTION HANDLING HARDENING — est. 2h

### 6.1 Top offenders
```
master_intelligence.py  9 naked except
rag.py                  8 naked except
queen_tools.py          5 naked except
prescout.py             5 naked except
orchestrator.py         4 naked except
graph_engine.py         4 naked except (usunięte w F5?)
falsifier.py            4 naked except
```

### 6.2 Pattern
```python
# Before:
except Exception:
    pass

# After:
except (ConnectionError, TimeoutError) as e:
    log.warning("Phase X failed: %s", e, exc_info=True)
    # Graceful degradation path
```

### 6.3 Categories of acceptable broad catch:
- Top-level orchestrator (catch-all for mission stability) — OK with logging
- Import fallbacks (`try: import X except ImportError`) — OK
- Everything else — NARROW to specific exceptions

**Zysk Fazy 6**: Better debugging, fewer silent failures

---

## FAZA 7: TEST COVERAGE BOOST (post-cleanup) — est. 4h

### 7.1 Coverage recalculation po cleanup
After removing ~3,500 LOC dead code:
- New total: ~28,000 LOC (was 31,673)
- Current covered: ~4,772 lines
- New coverage: 4,772 / 28,000 = **~17%** → BUT removing dead code also removes
  uncoverable lines, so actual new denominator from pytest-cov = ~10,600 → **~45%**

### 7.2 Fresh coverage push targets
Po cleanup, biggest remaining modules:
- `orchestrator.py` (~1,300 LOC after dedup) — async orchestration
- `synth.py` (~1,800 LOC after DuckDB cleanup) — report synthesis
- `process.py` (~2,200 LOC) — claim extraction
- `harvest.py` (~1,100 LOC) — async scraping
- `rag.py` (~1,800 LOC after DuckDB cleanup) — RAG pipeline

### 7.3 Strategy
- Expand `test_orch_harvest_35.py` with async mocks
- Queen._build_honey_context already well-tested → extend to _grade/_format
- Server endpoints already 14/14 tested → extend with edge cases
- CLI commands → parametrized testing all subcommands

**Target**: 50% coverage po Fazie 7

---

## FAZA 8: ASYNC ARCHITECTURE AUDIT — est. 2h

### 8.1 Problem
- `harvest.py`, `scout.py`, `prescout.py` = heavily async
- `orchestrator.py` = mostly sync, wraps async in `asyncio.run()`
- Potential event loop conflicts when called from within existing loop

### 8.2 Tasks
1. [ ] Map all `asyncio.run()` calls in orchestrator
2. [ ] Check for nested `asyncio.run()` (will deadlock)
3. [ ] Consider: should orchestrator be fully async?
4. [ ] Check: `behive serve` (FastAPI = already async) + orchestrator interaction

**Zysk Fazy 8**: No deadlocks, proper async flow

---

## FAZA 9: DEPENDENCY CLEANUP — est. 30 min

### 9.1 Remove unused optional deps
- `spacy>=3.7` in `[process]` — is it actually used?
- `crawl4ai>=0.3` in `[harvest]` — verify usage
- `nodriver>=0.38` in `[stealth]` — verify usage

### 9.2 Pin critical deps
- `litellm>=1.40` — good
- Add upper bounds for breaking-change-prone deps?

### 9.3 boto3 handling
- After Faza 3: boto3 should be fully OPTIONAL
- If kept: move to `[aws]` optional group
- Document: "Only needed if using AWS Bedrock as LLM provider"

---

## TIMELINE ESTIMATE

| Faza | Czas | LOC Impact | Coverage Impact | Risk |
|------|------|-----------|-----------------|------|
| 0. Quick Wins | 30 min | -440 | +1-2% | Minimal |
| 1. QueenPlanner dedup | 1.5h | -1,600 | +5% | Medium |
| 2. DuckDB removal | 3h | -500-800 | +3% | Medium |
| 3. BYOK compliance | 4h | -200 | +2% | High |
| 4. Legacy imports | 1.5h | ~0 | 0% | Low |
| 5. Graph engine | 2h | -1,210 | +4% | Medium |
| 6. Exception handling | 2h | ~0 | 0% | Low |
| 7. Test coverage | 4h | +500 (tests) | +10% | None |
| 8. Async audit | 2h | ~0 | 0% | Medium |
| 9. Deps cleanup | 30 min | ~0 | 0% | Low |
| **TOTAL** | **~21h** | **-4,000 LOC** | **+25%** | |

---

## EXPECTED END STATE

```
Before:                          After:
━━━━━━━━━━━━━━━━━━━━━━         ━━━━━━━━━━━━━━━━━━━━━━
LOC:       31,673                LOC:       ~27,500
Files:     47                    Files:     ~40
Coverage:  34%                   Coverage:  ~55-60%
DuckDB:    142 refs              DuckDB:    0
boto3:     65 direct calls       boto3:     0 (only in llm.py, optional)
Duplicates: 3× QueenPlanner     Duplicates: 0
Dead code: 440 LOC              Dead code: 0
BYOK:      Partially broken      BYOK:      100% compliant
Legacy:    92 hive2_* imports    Legacy:    0 (shims deprecated)
```

---

## DEPENDENCIES BETWEEN PHASES

```
F0 (Quick Wins) ─────────────┐
                              ├──→ F7 (Test Coverage)
F1 (QueenPlanner dedup) ─────┤
                              │
F2 (DuckDB removal) ─────────┤
         │                    │
         └── F5 (Graph Engine depends on DuckDB removal)
                              │
F3 (BYOK/boto3) ─────────────┤
                              │
F4 (Legacy imports) ──────────┘
         │
         └── Can run after F1 (needs stable module names)

F6 (Exceptions) ── Independent, can run anytime
F8 (Async audit) ── After F2+F3 (architecture stabilized)
F9 (Deps) ── After F3 (boto3 decision made)
```

**Optimal execution order**: F0 → F1 → F2 → F5 → F3 → F4 → F6 → F9 → F7 → F8

---

## RISKS & MITIGATIONS

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| bionic_quorum breaks (uses DuckDB actively) | High | Medium | Rewrite queries to PG BEFORE removing DuckDB |
| QueenPlanner dedup picks wrong version | Medium | High | Git blame all 3, pick most-recently-patched |
| boto3 removal breaks running missions | Medium | High | Stage: first route new calls through llm.py, then remove old paths |
| Tests break after large removals | High | Low | Run suite after EVERY file deletion |
| graph_engine removal breaks MCP | Low | Medium | Check MCP tool implementations first |

---

## ACCEPTANCE CRITERIA (Definition of Done)

- [ ] `grep -rn 'duckdb\|DuckDB' src/behive/` = 0
- [ ] `grep -rn 'import boto3' src/behive/ | grep -v llm.py` = 0
- [ ] `grep -rn 'from hive2_' src/behive/` = 0
- [ ] `class QueenPlanner` defined exactly 1× in codebase
- [ ] `BEHIVE_MODEL=gpt-4o behive doctor` — no errors
- [ ] Coverage ≥ 50%
- [ ] `behive serve` starts clean
- [ ] No secrets in any commit (`git log --all -p | grep -c 'password.*='` stable)
- [ ] All 790+ existing tests still pass
