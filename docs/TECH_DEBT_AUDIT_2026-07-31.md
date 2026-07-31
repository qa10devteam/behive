# BeHive — Pełny Audyt Długu Technologicznego i Projektowego
## Data: 2026-07-31 | Wersja: 0.4.0 | 31,673 LOC | 47 plików Python

---

## 🔴 KRYTYCZNE (blokują skalowanie i wiarygodność)

### 1. DuckDB Legacy — 142 referencje w 16 plikach
**Problem**: Projekt deklaruje PostgreSQL + Qdrant, ale DuckDB żyje w:
- `rag.py` — DuckDB VSS jako "fallback" (21 refs), `_haiku_call` bezpośredni
- `bionic_quorum.py` — `_duckdb_connect()` aktywnie używany (5 refs)
- `prescout.py` (12), `orchestrator.py` (10), `queen_primer.py` (9)
- `domain_reputation.py` (6), `synth.py` (5), `queen_tools.py` (3)
- `scout.py`, `process.py`, `harvest.py`, `events.py`, `db_helpers.py`, `content_quality.py` (1-2 each)

**Wpływ**: Martwy kod, fałszywy fallback path, confusing architektura.
**Fix**: Usunąć WSZYSTKIE duckdb referencje. Qdrant = vector store. PostgreSQL = relational.

---

### 2. Direct Bedrock/boto3 — 65 wywołań POZA llm.py (naruszenie BYOK)
**Problem**: BYOK obiecuje "any LLM via litellm", ale 9 plików omijają `llm.py`:
- `rag.py` — `_haiku_call()`, `_get_bedrock()`, direct `invoke_model`
- `queen_feedback.py` — hardcoded `MODEL_HAIKU`, direct boto3
- `intel_summary.py` — `_bedrock_fallback()` z hardcoded model ID
- `falsifier.py` — `BEDROCK_MODEL`, direct boto3 + OpenAI compat
- `queen_fact_mem.py` — `_haiku()` z direct boto3
- `graph_engine.py`, `master_intelligence.py`, `process.py` — boto3 imports
- `bedrock_compat.py` — DEAD CODE (imported by nobody)

**Hardcoded model IDs** (will break when versions rotate):
```
eu.anthropic.claude-haiku-4-5-20251001-v1:0  (5 files)
eu.anthropic.claude-sonnet-4-6               (1 file)
us.anthropic.claude-haiku-4-5-20251001-v1:0  (1 file)
```

**Wpływ**: User z `BEHIVE_MODEL=gpt-4o` dostaje crashes z boto3 ImportError.
Ktoś bez AWS credentials nie może używać 40% pipeline'u.
**Fix**: Route ALL LLM calls through `llm.py` → litellm. Zero direct boto3.

---

### 3. QueenPlanner zdefiniowany 3× (trzy kopie klasy!)
**Problem**: `class QueenPlanner` istnieje w:
1. `orchestrator.py:206` — 800+ linii
2. `queen.py:22` — 839 linii
3. `queen_planner_extracted.py:21` — 838 linii

`queen.py` i `queen_planner_extracted.py` różnią się ~53 liniami (komentarze).
`orchestrator.py` ma pełną trzecią kopię.

**Wpływ**: Patchujesz bug w jednej — dwie pozostałe mają go dalej.
**Fix**: Jedna kanonowana `QueenPlanner` (w `orchestrator.py` lub osobnym pliku), reszta importuje.

---

### 4. quality.py ≡ content_quality.py (pełny duplikat)
**Problem**: Dwa pliki (279 + 275 LOC) z identycznymi funkcjami:
- `score_content_detailed(text)` — ta sama logika
- `score_content(text)` — identyczna
- `is_covert_infected(text)` — identyczna
- `quality_label(score)` — identyczna

Różnica: 4 linie komentarzy (PL vs EN). Oba pliki są importowane via compat shims.

**Wpływ**: 554 LOC inflacji. Testy muszą pokrywać oba. Bugi fixowane w jednym.
**Fix**: Usunąć `quality.py`, shim `hive2_content_quality → content_quality.py`.

---

## 🟠 WYSOKI PRIORYTET

### 5. Legacy `hive2_*` imports — 92 referencje w produkcyjnym kodzie
**Problem**: Moduły engine wewnętrznie importują `from hive2_queen_memory import ...`
zamiast `from behive.engine.queen_memory import ...`.
Zależą od runtime shim installation (`install_shims()`).

**Wpływ**: Jeden brakujący `install_shims()` → ImportError w runtime.
Confusing DX — nowy contributor nie rozumie czemu import z `hive2_*` działa.
**Fix**: Find-replace wszystkie `hive2_*` → `behive.engine.*`. Potem usunąć shims.

---

### 6. runner.py — Dead Code (147 LOC, nigdy nie importowany)
**Problem**: `src/behive/engine/runner.py` — nie jest importowany przez ŻADEN inny moduł.
Definiuje `run_phase()` ale nikt tego nie woła.
**Fix**: Usunąć lub zintegrować z orchestratorem.

---

### 7. bedrock_compat.py — Dead Code (146 LOC, importowane przez NIKOGO)
**Problem**: `BedrockCompatClient`, `patch_boto3_bedrock()`, `unpatch_boto3()` —
zdefiniowane, nigdy używane nigdzie w codebase.
**Fix**: Usunąć.

---

### 8. Broad Exception Swallowing — 47+ naked `except Exception:`
Top offenders: `master_intelligence.py` (9), `rag.py` (8), `queen_tools.py` (5),
`prescout.py` (5), `orchestrator.py` (4).
**Wpływ**: Bugs silently disappear. Hard to debug. Logs full of "non-critical: ..." noise.
**Fix**: Replace with specific exceptions, log with `exc_info=True`.

---

## 🟡 ŚREDNI PRIORYTET

### 9. Version Inconsistency
- `pyproject.toml`: `0.4.0`
- `__init__.py` fallback: `0.3.3`
**Fix**: Sync fallback version.

### 10. Compat Shims map non-existent modules
`SHIM_MAP` references `behive.engine.cluster` and `behive.engine.scout_targeted` —
these files DON'T EXIST in `src/behive/engine/`. Silent pass on ImportError.
**Fix**: Clean SHIM_MAP or create those modules.

### 11. Async/Sync Mismatch
- `harvest.py` (26 async defs), `scout.py` (24), `prescout.py` (20)
- Ale `orchestrator.py` (1 async!) — woła te moduły synchronicznie via `asyncio.run()`?
**Wpływ**: Potential event loop conflicts, hard-to-debug deadlocks.
**Needs audit**: Jak orchestrator woła async harvest/scout/process?

### 12. graph_engine.py — DuckDB → NetworkX pipeline (1210 LOC)
Docstring mówi: "DuckDB → Entity Resolution → NetworkX → HDBSCAN → Qdrant"
**Problem**: Jeśli DuckDB jest dead, to cały graph_engine path jest broken.
**Fix**: Przepisać na PostgreSQL → NetworkX → Qdrant (lub usunąć jeśli unused).

### 13. knowledge_graph.py vs graph_engine.py overlap
Dwa osobne pliki do grafów:
- `knowledge_graph.py` (poza engine/) — Neo4j
- `engine/graph_engine.py` — NetworkX + DuckDB + Qdrant
**Fix**: Consolidate.

---

## 📊 STATYSTYKI DŁUGU

| Kategoria | Ilość |
|-----------|-------|
| DuckDB refs (martwy kod) | 142 |
| Direct boto3 calls (BYOK violation) | 65 |
| Legacy hive2_* imports | 92 |
| Hardcoded model IDs | 7 |
| Duplicate classes (QueenPlanner ×3) | 3 kopie |
| Duplicate files (quality ≡ content_quality) | 554 LOC |
| Dead code files (runner, bedrock_compat) | 293 LOC |
| Broad exception: handlers | 47+ |
| Non-existent shim targets | 2 |
| TODO/FIXME markers | 1 (surprisingly low) |
| Total estimated dead/duplicate LOC | ~3,500-4,000 |

---

## 🎯 RECOMMENDED CLEANUP ORDER

1. **Usunąć quality.py** (duplikat content_quality.py) — 5 min
2. **Usunąć runner.py + bedrock_compat.py** (dead code) — 5 min
3. **Usunąć DuckDB** z 16 plików — 2-3h (biggest impact)
4. **Route ALL boto3 → llm.py** (BYOK compliance) — 3-4h
5. **Deduplicate QueenPlanner** (pick one canonical, delete 2) — 1h
6. **Replace hive2_* imports** z bezpośrednimi — 1h (then delete shims)
7. **Fix version** — 5 min
8. **Audit graph_engine.py** — decide: rewrite on PG or delete — 1-2h
9. **Tighten exception handlers** — ongoing

**Szacowany zysk**: -3500 LOC, +15% coverage (mniej kodu do pokrycia),
eliminacja #1 user complaint (boto3 crash for non-AWS users).
