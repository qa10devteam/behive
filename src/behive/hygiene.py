"""Mission/claim hygiene.

Empty-done is a lie: status=done requires clean claims with mission provenance.
JSON/list envelopes from v4 extraction are not claims.
"""
import re


def is_structured_blob(text) -> bool:
    if text is None:
        return False
    s = str(text).lstrip()
    return bool(s) and s[0] in "{["


# PostgreSQL predicate on hive_claims.claim
SQL_CLAIM_NOT_BLOB = "left(ltrim(claim), 1) NOT IN ('{', '[')"


# Pipeline exit code is not a product. Claims are.
MIN_CLAIMS_FOR_DONE = 3


# Completion queries use alias c. URL provenance is mission-scoped: API scouts
# legitimately write hive_content without inserting any hive_sources rows.
SQL_COMPLETION_CLAIM = """
    (c.is_garbage = false OR c.is_garbage IS NULL)
    AND length(ltrim(COALESCE(c.claim, ''), chr(32) || chr(9) || chr(10) || chr(13) || chr(11) || chr(12))) > 0
    AND substr(ltrim(c.claim, chr(32) || chr(9) || chr(10) || chr(13) || chr(11) || chr(12)), 1, 1) NOT IN ('{', '[')
    AND length(trim(COALESCE(c.source_url, ''))) > 0
    AND (
        EXISTS (SELECT 1 FROM hive_sources s
                WHERE s.mission_id = c.mission_id AND s.url = c.source_url)
        OR EXISTS (SELECT 1 FROM hive_content d
                   WHERE d.mission_id = c.mission_id AND d.url = c.source_url)
    )
"""

# Useful claims decide 'done': eligible above AND not deterministic regex salvage.
# regex_fallback sentences (claims_fallback) may stay as material, but a mission
# whose only claims are regex salvage is not a research result (diag 2026-11-12:
# 28 of 43 'done' missions in 30 days had no report or only regex_fallback).
SQL_USEFUL_CLAIM = SQL_COMPLETION_CLAIM + """    AND COALESCE(c.claim_type, '') <> 'regex_fallback'
"""

_SQL_WHITESPACE = "chr(32) || chr(9) || chr(10) || chr(13) || chr(11) || chr(12)"


def sql_report_present(mission: str = "hive_missions") -> str:
    """Predicate: the mission row (table or alias ``mission``) has a non-blank report."""
    return f"length(ltrim(COALESCE({mission}.synthesis, ''), {_SQL_WHITESPACE})) > 0"


def sql_done_product(mission: str = "hive_missions") -> str:
    """Terminal 'done' authority: a non-blank report AND at least one useful claim."""
    return (f"({sql_report_present(mission)} AND EXISTS (SELECT 1 FROM hive_claims c "
            f"WHERE c.mission_id = {mission}.id AND {SQL_USEFUL_CLAIM}))")


# Phase failures that end a mission as error, whatever material exists.
PROCESS_FAILURE_PHASES = frozenset({"process_timeout", "process_error"})


def classify_completion(*, useful_claims: int, report_present: bool,
                        sources: int, content: int) -> tuple[str, str]:
    """(status, phase) of a pipeline that ran to its end without a phase failure."""
    if int(useful_claims or 0) > 0 and report_present:
        return "done", "done"
    if int(useful_claims or 0) > 0:
        return "error", "synthesis_failed"
    if int(sources or 0) <= 0 and int(content or 0) <= 0:
        return "empty", "empty_scout"
    return "empty", "empty_claims"


# Public, payload-free explanations (never exception text or provider output).
MISSION_REASONS = {
    "empty_scout": "No sources or qualifying claims were produced.",
    "empty_claims": "Sources were collected but no qualifying claims were produced.",
    "process_timeout": "The processing phase exceeded its time limit; no report was produced.",
    "process_error": "The processing phase failed; no report was produced.",
    "synthesis_failed": "Qualifying claims were found but no report was produced.",
    "pipeline_failed": "Pipeline execution failed without a qualifying product.",
    "claims_fallback_failed": "Claims recovery failed without a qualifying product.",
    "pipeline_exception": "Pipeline execution raised an exception.",
    "timeout": "Pipeline execution timed out.",
    "cancelled": "Mission was cancelled.",
}
_STATUS_REASONS = {
    "empty": "Mission finished without qualifying claims.",
    "error": "Mission execution failed.",
    "cancelled": "Mission was cancelled.",
}
LEGACY_DONE_REASON = ("Marked done without a report or without claims beyond regex fallback "
                      "(recorded before the 2026-11-13 status rules); it has no usable result.")


def mission_reason(status, phase, *, report_present: bool | None = None,
                   useful_claims: int | None = None) -> str | None:
    """Why a mission has no (usable) report; None for in-progress and real done."""
    if status in _STATUS_REASONS:
        return MISSION_REASONS.get(phase) or _STATUS_REASONS[status]
    if status == "done" and (report_present is False or useful_claims == 0):
        return LEGACY_DONE_REASON
    return None


def is_empty_mission(sources: int, claims: int, *, sourced_claims: int | None = None) -> bool:
    """Require clean claims with provenance, including API-only content.

    Callers with actual evidence supply sourced_claims. Legacy count-only callers
    retain the conservative source+claim guard; counts cannot prove API provenance.
    """
    if sourced_claims is not None:
        return int(claims or 0) <= 0 or int(sourced_claims or 0) <= 0
    return int(sources or 0) <= 0 or int(claims or 0) <= 0


def has_salvageable_claims(claims: int) -> bool:
    """A nonzero exit produced enough eligible claims (not raw row counts)."""
    return int(claims or 0) >= MIN_CLAIMS_FOR_DONE


_CSS = re.compile(
    r"[{}]|padding-|font-size|#dropdown|border:|width:\s*\d|margin-right|float:\s*left|"
    r"/\*|javascript:|lorem ipsum",
    re.I,
)
_HTML = re.compile(r"<[^>]+>")
_SENT_START = re.compile(r"[A-ZĄĆĘŁŃÓŚŹŻ]")


def sanitize_claim(text: str, *, require_digit: bool = True) -> str | None:
    """Drop CSS/HTML/truncated fragments. Keep a full sentence.

    Deterministic (no-model) writers keep ``require_digit=True`` as their
    garbage guard. Model source extraction passes ``False``: its claims are
    already bounded by verbatim-quote and confidence checks, and supplier facts
    ("Firma X oferuje system WMS Y") often contain no number.
    """
    s = _HTML.sub(" ", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 40:
        return None
    if is_structured_blob(s) or _CSS.search(s):
        return None
    if s.count(";") >= 5:
        return None
    m = _SENT_START.search(s)
    if not m:
        return None
    s = s[m.start():].strip()
    if len(s) < 40 or len(s) > 800:
        return None
    if s[0].islower() or s[0] in ")]},.;:":
        return None
    if require_digit and not re.search(r"\d", s):
        return None
    return s
