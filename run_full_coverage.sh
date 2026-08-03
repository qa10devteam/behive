#!/bin/bash
# run_full_coverage.sh — runs ALL tests in batches (avoids xdist OOM) and reports combined coverage
set -e
cd /home/ubuntu/behive-pkg

PYTEST="/home/ubuntu/stealth-research-env/bin/pytest"
COV_ARGS="--cov=behive.engine --cov-report= --cov-append -q --tb=no --timeout=10 -W ignore"

# Clean previous coverage data
rm -f .coverage .coverage.*

# Batch 1: test_80_* (our main test battery)
$PYTEST tests/test_80_*.py $COV_ARGS -n 8 --timeout=15 2>/dev/null || true

# Batch 2: test_cov_* (existing coverage tests)
$PYTEST tests/test_cov_pure_functions.py tests/test_cov_utilities_deep.py tests/test_cov_synth.py tests/test_cov_synth_deep.py tests/test_cov_prescout_harvest_pure.py tests/test_cov_graph.py tests/test_cov_graph_rag.py tests/test_cov_queen_tools.py tests/test_cov_targeted_modules.py tests/test_cov_strategic.py tests/test_cov_deep.py tests/test_cov_deep2.py tests/test_cov_deep3.py tests/test_cov_harvest.py tests/test_cov_breakthrough.py tests/test_cov_breakthrough2.py tests/test_cov_orchestrator_deep.py tests/test_cov_import_push.py tests/test_cov_master.py tests/test_cov_queen_scout_full.py tests/test_cov_drones_direct.py tests/test_cov_process.py tests/test_cov_process_deep.py tests/test_cov_40pct_push.py tests/test_cov_push50.py tests/test_cov_final50.py tests/test_cov_final_push.py tests/test_cov_ceiling_breaker.py tests/test_cov_massive.py tests/test_cov_mock_pool.py tests/test_cov_real_execution.py tests/test_cov_harvest_llm_api.py tests/test_cov_server_client.py tests/test_cov_server_endpoints.py tests/test_cov_zero_modules.py tests/test_cov_falsifier.py tests/test_cov_processing_drones.py tests/test_cov_process_deep2.py tests/test_cov_prescout.py tests/test_cov_orchestrator_full.py tests/test_cov_rag_master.py tests/test_cov_drones.py tests/test_cov_async_deep.py tests/test_cov_async_push.py tests/test_coverage_phase7.py tests/test_branch_coverage.py $COV_ARGS 2>/dev/null || true

# Batch 3: core module tests
$PYTEST tests/test_constants.py tests/test_config.py tests/test_content_quality.py tests/test_domain_reputation.py tests/test_events.py tests/test_falsifier.py tests/test_guardrail.py tests/test_llm.py tests/test_preprocessor.py tests/test_db.py tests/test_smoke.py tests/test_api_scout.py tests/test_harvest.py tests/test_prescout.py tests/test_process.py tests/test_orchestrator.py tests/test_rag.py tests/test_scout.py tests/test_synth.py tests/test_search_backends.py tests/test_graph_engine.py $COV_ARGS 2>/dev/null || true

# Batch 4: integration & deep tests
$PYTEST tests/test_functional.py tests/test_deep_functions.py tests/test_deep_execution.py tests/test_bionic_quorum.py tests/test_drones.py tests/test_beeworker_deep.py tests/test_queen_deep.py tests/test_queen_honey.py tests/test_client.py tests/test_cli.py tests/test_server.py tests/test_integration_mocked.py tests/test_regression.py tests/test_uncovered_functions.py $COV_ARGS 2>/dev/null || true

# Batch 5: mega/heavy/real tests
$PYTEST tests/test_mega_coverage.py tests/test_heavy_coverage.py tests/test_real_heavy.py tests/test_real_integration.py tests/test_db_integration.py tests/test_mcp.py tests/test_server_and_more.py tests/test_server_integration.py tests/test_knowledge_graph.py tests/test_e2e_pipeline.py $COV_ARGS 2>/dev/null || true

# Batch 6: 35% push tests
$PYTEST tests/test_blast_coverage.py tests/test_lowest_coverage.py tests/test_push_30pct.py tests/test_final_30pct.py tests/test_gap_fill_35.py tests/test_cross_35.py tests/test_micro_35.py tests/test_mass_coverage_35.py tests/test_orch_harvest_35.py tests/test_prescout_deep_35.py tests/test_push_35_final.py tests/test_server_cli_35.py tests/test_process_deep.py tests/test_process_orch_deep.py tests/test_rag_orch_30pct.py tests/test_queen_load_persist.py tests/test_runpy_main.py $COV_ARGS 2>/dev/null || true

# Final report
/home/ubuntu/stealth-research-env/bin/coverage report --include="src/behive/engine/*" | grep "^TOTAL"
