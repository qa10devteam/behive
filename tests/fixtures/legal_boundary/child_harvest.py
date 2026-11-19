"""Child process of tests/test_diag1117_legal_boundary_pg.py: the real harvest CLI, recorded transport.

Runs behive.engine.harvest.main() (argv: mission id) against the private PostgreSQL named by
LEGAL_BOUNDARY_DSN. Nothing here changes production code or what the harvest decides:

  * the three L1 fetchers (legal_sources.fetch_celex_text / fetch_eli_act / fetch_sejm_print) are
    replaced by a replay of tests/fixtures/legal_boundary/transport.json (their boundary);
  * every generic extractor (web, jina, crawl4ai, newspaper, pdf, document, json) is replaced by a
    spy that records the URL and returns an empty failure, so a row that leaks onto the generic
    path is visible instead of reaching the network;
  * socket.getaddrinfo and every AF_INET/AF_INET6 connect are blocked and recorded.

The spy (JSON) is written to LEGAL_BOUNDARY_SPY when the harvest ends, also after a crash. On a tree
without behive.engine.legal_sources (the deployed code) the fetchers are simply not replaced.
"""
import json
import logging
import os
import socket
import sys


def _build_text(spec: dict) -> str:
    lines = [spec["template"].format(i=i, j=i + 10) for i in range(1, int(spec["count"]) + 1)]
    body = " ".join(lines)
    return f"<html><body><p>{body}</p></body></html>" if spec.get("kind") == "html" else body


def main() -> None:
    mission = sys.argv[1]
    with open(os.environ["LEGAL_BOUNDARY_TRANSPORT"], encoding="utf-8") as fh:
        transport = json.load(fh)
    spy_path = os.environ["LEGAL_BOUNDARY_SPY"]
    spy: dict = {"network": [], "fetch": [], "generic": [], "legal_sources": False}

    # -- no network: record and refuse --------------------------------------------------------
    def _getaddrinfo(host, *args, **kwargs):
        spy["network"].append(f"getaddrinfo:{host}")
        raise socket.gaierror("network is blocked in the legal boundary test")

    real_connect = socket.socket.connect

    def _connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            spy["network"].append(f"connect:{address[0]}")
            raise OSError("network is blocked in the legal boundary test")
        return real_connect(self, address)

    socket.getaddrinfo = _getaddrinfo
    socket.socket.connect = _connect

    # -- database: the private schema, as the live_db fixture does in-process -----------------
    from behive.engine import db as engine_db

    engine_db.DSN = os.environ["LEGAL_BOUNDARY_DSN"]
    engine_db._pool = None

    from behive.engine import harvest

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(name)s %(levelname)s %(message)s", force=True)
    harvest.clear_url_cache()
    harvest._get_drone_strategy = lambda mission_id, domain: {"strategy": "DIRECT"}

    # -- generic extractors: spies -------------------------------------------------------------
    def _async_spy(name):
        async def _spy(self, url, *a, **k):
            spy["generic"].append({"extractor": name, "url": url})
            return {"method": name, "text": "", "word_count": 0, "success": False}
        return _spy

    def _sync_spy(name):
        def _spy(self, url, *a, **k):
            spy["generic"].append({"extractor": name, "url": url})
            return {"method": name, "text": "", "word_count": 0, "success": False}
        return _spy

    for name in ("_harvest_web", "_harvest_jina", "_harvest_crawl4ai", "_harvest_newspaper"):
        setattr(harvest.HarvesterBee, name, _async_spy(name))
    for name in ("_harvest_pdf", "_harvest_document", "_harvest_json"):
        setattr(harvest.HarvesterBee, name, _sync_spy(name))

    # -- the L1 fetchers: replay of the recorded transport -------------------------------------
    try:
        from behive.engine import legal_sources as ls
    except ImportError:
        ls = None
    if ls is not None:
        spy["legal_sources"] = True

        def _replay(kind: str, source: str, key: str):
            rec = transport[kind].get(key)
            spy["fetch"].append({"kind": kind, "id": key, "recorded": rec is not None,
                                 "fetch_url": (rec or {}).get("fetch_url", "")})
            if rec is None:
                return ls.LegalFetch(False, source, key, "", "", "", 0, 0, "", "not_recorded")
            text = _build_text(rec["text"]) if rec.get("ok") else ""
            size = rec.get("byte_size") or len(text.encode("utf-8"))
            return ls.LegalFetch(bool(rec["ok"]), rec["source"], key, rec["title"], rec["fetch_url"],
                                 rec["content_type"], int(rec["http_status"]), int(size), text,
                                 rec.get("reason", ""))

        ls.fetch_celex_text = lambda celex, langs=("POL", "ENG"): _replay("celex", "cellar", celex)
        ls.fetch_eli_act = lambda publisher, year, pos: _replay("eli", "eli", f"{publisher}/{year}/{pos}")
        ls.fetch_sejm_print = lambda term, number: _replay("print", "sejm_print", f"{term}/{number}")

    sys.argv = ["harvest", mission]
    try:
        harvest.main()
    finally:
        with open(spy_path, "w", encoding="utf-8") as fh:
            json.dump(spy, fh)


if __name__ == "__main__":
    main()
