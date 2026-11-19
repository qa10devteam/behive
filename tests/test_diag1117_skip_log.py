"""Skip log: one INFO line per skipped source with a fixed reason code, one summary per mission."""
import logging

import pytest

LOGGER = "hive2.harvest"


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


def test_note_skip_logs_one_info_line_and_counts(caplog):
    from behive.engine import harvest

    caplog.set_level(logging.INFO, logger=LOGGER)
    counts: dict[str, int] = {}
    harvest.note_skip("m1", "https://a.example.test/x", "score_filter", "relevance=3 total=40", counts)
    harvest.note_skip("m1", "https://b.example.test/y", "score_filter", "relevance=9 total=10", counts)
    harvest.note_skip("m1", "https://c.example.test/z", "domain_blacklist", "too many\nfailures", counts)

    assert counts == {"score_filter": 2, "domain_blacklist": 1}
    assert _info(caplog) == [
        "skip mission=m1 reason=score_filter url=https://a.example.test/x detail=relevance=3 total=40",
        "skip mission=m1 reason=score_filter url=https://b.example.test/y detail=relevance=9 total=10",
        "skip mission=m1 reason=domain_blacklist url=https://c.example.test/z detail=too many failures",
    ]


def test_reason_codes_are_fixed(caplog):
    from behive.engine import harvest

    assert {"score_filter", "domain_blacklist", "prescout_skip", "drone_skip", "cache_skip"} <= set(
        harvest.SKIP_REASONS
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    counts: dict[str, int] = {}
    with pytest.raises(ValueError):
        harvest.note_skip("m1", "https://a.example.test/x", "because I felt like it", "", counts)
    assert counts == {} and _info(caplog) == []


def test_skip_summary_is_one_info_line_and_adds_up(caplog):
    from behive.engine import harvest

    caplog.set_level(logging.INFO, logger=LOGGER)
    counts = {"score_filter": 44, "domain_blacklist": 15}
    harvest.skip_summary("m1", counts, loaded=10, groomed=15)
    lines = [m for m in _info(caplog) if m.startswith("skip_summary")]
    assert lines == [
        "skip_summary mission=m1 loaded=10 groomed=15 skipped=59 score_filter=44 "
        "domain_blacklist=15 prescout_skip=0 drone_skip=0 cache_skip=0"
    ]
