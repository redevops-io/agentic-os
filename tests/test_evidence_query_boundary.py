"""D-1 evidence-query boundary: authorized expressive SQL works; every route to a host file, a second scope or a
mutation fails; the FILESYSTEM SANDBOX (worker process, no external access, locked configuration) is the boundary and
the statement guard is defence in depth (so the escape vectors are also run with the guard DISABLED)."""
from __future__ import annotations

import http.server
import os
import socket
import threading

import pytest

from agentic_os.evidence_query import (
    DatasetSpec, Limits, QueryRefused, ScopeCatalog, ViewSpec, worker as W)
from agentic_os.evidence_query.guard import StatementRejected, check_statement
from tests.evidence_query_support import (
    ALPHA_ROWS, BETA_ROWS, CANARY, EVENT_COLS, make_catalog, make_service, owner, stranger, tree_state)


# ───────────────────────── authorized, expressive SQL keeps working ─────────────────────────
def test_joins_ctes_windows_aggregates_unnest_json(tmp_path):
    svc, _ = make_service(tmp_path)
    o = owner()
    r = svc.query(o, "alpha", "SELECT capability, count(*) n, sum(amount) s FROM events GROUP BY capability ORDER BY capability")
    assert r.columns == ["capability", "n", "s"] and r.rows == [["crm.read", 2, 40.0], ["storage.upload", 1, 20.0]]
    r = svc.query(o, "alpha", "WITH d AS (SELECT mission_id FROM events WHERE verdict='DENY') "
                              "SELECT e.mission_id, e.capability FROM events e JOIN d ON d.mission_id = e.mission_id ORDER BY 2")
    assert r.rows == [["m-a1", "crm.read"], ["m-a1", "storage.upload"]]
    r = svc.query(o, "alpha", "SELECT mission_id, amount, sum(amount) OVER (ORDER BY amount) AS running, "
                              "row_number() OVER (PARTITION BY mission_id ORDER BY amount) rn FROM events ORDER BY amount")
    assert [x[2] for x in r.rows] == [10.0, 30.0, 60.0]
    r = svc.query(o, "alpha", "SELECT mission_id, json_extract_string(payload, '$.k[0]') AS first FROM events ORDER BY amount")
    assert [x[1] for x in r.rows] == ["1", "3", None]
    r = svc.query(o, "alpha", "SELECT x FROM (SELECT unnest([1,2,3]) AS x) t ORDER BY x")
    assert r.rows == [[1], [2], [3]]
    r = svc.query(o, "alpha", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<5) SELECT sum(n) FROM r")
    assert r.rows == [[15]]
    r = svc.query(o, "alpha", "SELECT * FROM denials")                       # the registered view
    assert r.rows == [["m-a1", "storage.upload"]]
    r = svc.query(o, "alpha", "SELECT i FROM range(3) t(i) WHERE i IN (SELECT 1)")
    assert r.rows == [[1]]


def test_result_is_a_projection_of_the_scope_only(tmp_path):
    svc, _ = make_service(tmp_path)
    r = svc.query(owner(), "alpha", "SELECT DISTINCT tenant FROM events")
    assert r.rows == [["alpha"]]


# ───────────────────────── authentication / authorization before any query ─────────────────────────
def test_no_principal_is_refused(tmp_path):
    svc, sink = make_service(tmp_path)
    with pytest.raises(QueryRefused) as e:
        svc.query(None, "alpha", "SELECT 1")
    assert e.value.code == "unauthenticated"
    assert sink.receipts[-1]["outcome"] == "refused"


def test_unauthorized_and_unknown_scope_are_indistinguishable(tmp_path):
    svc, sink = make_service(tmp_path)
    with pytest.raises(QueryRefused) as a:
        svc.query(stranger(), "alpha", "SELECT 1")
    with pytest.raises(QueryRefused) as b:
        svc.query(owner(), "no-such-scope", "SELECT 1")
    with pytest.raises(QueryRefused) as c:
        svc.query(owner(), "beta", "SELECT 1")               # owner holds alpha only
    assert (a.value.code, str(a.value)) == (b.value.code, str(b.value)) == (c.value.code, str(c.value))
    reasons = [r["reason"] for r in sink.receipts]
    assert reasons == ["not_authorized", "unknown_scope", "not_authorized"]       # distinguished only in the audit


def test_permission_plane_names_and_separation(tmp_path):
    svc, _ = make_service(tmp_path, grants=(("owner", "evidence.alpha.reconstruct"),))
    with pytest.raises(QueryRefused):                        # a reconstruct grant is not an inspect grant
        svc.query(owner(), "alpha", "SELECT 1")
    svc2, _ = make_service(tmp_path / "b", grants=(("owner", "evidence.alpha"),))     # dotted prefix covers inspect
    assert svc2.query(owner(), "alpha", "SELECT 1").rows == [[1]]
    svc3, _ = make_service(tmp_path / "c", grants=(("owner", "evidence.alpha.inspect"),))
    with pytest.raises(QueryRefused):
        svc3.query(owner(), "beta", "SELECT 1")
    with pytest.raises(QueryRefused):                         # a sibling scope name is not a prefix match
        make_service(tmp_path / "d", grants=(("owner", "evidence.alph"),))[0].query(owner(), "alpha", "SELECT 1")


def test_ceiling_that_raises_denies(tmp_path):
    svc, _ = make_service(tmp_path)
    class Boom:
        def allows(self, *a): raise RuntimeError("boom")
    svc.ceiling = Boom()
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT 1")
    assert e.value.code == "not_found"


def test_sealed_grant_store_denies(tmp_path):
    svc, _ = make_service(tmp_path)
    (tmp_path / "perm" / ("grants.enc" if (tmp_path / "perm" / "grants.enc").exists() else "grants.jsonl")).write_bytes(b"tampered")
    with pytest.raises(QueryRefused):
        svc.query(owner(), "alpha", "SELECT 1")


def test_scope_ids_are_restricted(tmp_path):
    cat = ScopeCatalog()
    for bad in ("a.b", "A", "", "x y", "../x", "a" * 80):
        with pytest.raises(ValueError):
            cat.register_scope(bad, [DatasetSpec("t", (("x", "VARCHAR"),), rows=[])])


# ───────────────────────── every host-file / mutation vector ─────────────────────────
def _vectors(canary_path, out_path, out_dir, port):
    p, o, d = canary_path, out_path, out_dir
    return {
        "read_text": f"SELECT * FROM read_text('{p}')",
        "read_csv": f"SELECT * FROM read_csv('{p}')",
        "read_csv_auto": f"SELECT * FROM read_csv_auto('{p}')",
        "read_parquet": f"SELECT * FROM read_parquet('{p}')",
        "read_json": f"SELECT * FROM read_json('{p}')",
        "read_json_auto": f"SELECT * FROM read_json_auto('{p}')",
        "read_blob": f"SELECT * FROM read_blob('{p}')",
        "read_ndjson": f"SELECT * FROM read_ndjson('{p}')",
        "glob": f"SELECT * FROM glob('{os.path.dirname(p)}/*')",
        "glob_etc": "SELECT * FROM glob('/etc/*')",
        "etc_hosts": "SELECT * FROM read_text('/etc/hosts')",
        "replacement_scan": f"SELECT * FROM '{p}'",
        "replacement_scan_csv": f"SELECT * FROM '{p}.csv'",
        "copy_to": f"COPY (SELECT 1 a) TO '{o}'",
        "copy_table_to": f"COPY events TO '{o}' (FORMAT CSV)",
        "copy_from": f"COPY events FROM '{p}'",
        "export_database": f"EXPORT DATABASE '{d}'",
        "import_database": f"IMPORT DATABASE '{d}'",
        "attach": f"ATTACH '{o}.db' AS x",
        "attach_canary": f"ATTACH '{p}' AS x (READ_ONLY)",
        "detach": "DETACH DATABASE memory",
        "install_httpfs": "INSTALL httpfs",
        "load_httpfs": "LOAD httpfs",
        "load_path": f"LOAD '{p}'",
        "force_install": "FORCE INSTALL httpfs",
        "set_external": "SET enable_external_access = true",
        "set_lock": "SET lock_configuration = false",
        "set_tempdir": f"SET temp_directory = '{d}'",
        "set_home": f"SET home_directory = '{d}'",
        "set_allowed": f"SET allowed_directories = ['{os.path.dirname(p)}']",
        "set_autoload": "SET autoload_known_extensions = true",
        "pragma": "PRAGMA enable_external_access",
        "pragma_set": "PRAGMA threads = 8",
        "pragma_db_list": "SELECT * FROM pragma_database_list()",
        "reset": "RESET enable_external_access",
        "getenv": "SELECT getenv('HOME')",
        "current_setting": "SELECT current_setting('home_directory')",
        "current_setting_tmp": "SELECT current_setting('temp_directory')",
        "duckdb_settings": "SELECT * FROM duckdb_settings()",
        "duckdb_tables": "SELECT * FROM duckdb_tables()",
        "duckdb_views": "SELECT sql FROM duckdb_views()",
        "duckdb_extensions": "SELECT * FROM duckdb_extensions()",
        "duckdb_databases": "SELECT * FROM duckdb_databases()",
        "duckdb_functions": "SELECT * FROM duckdb_functions() LIMIT 1",
        "information_schema": "SELECT * FROM information_schema.tables",
        "information_schema_cols": "SELECT * FROM information_schema.columns",
        "sqlite_master": "SELECT * FROM sqlite_master",
        "url_csv": f"SELECT * FROM read_csv('http://127.0.0.1:{port}/x.csv')",
        "url_parquet": f"SELECT * FROM 'http://127.0.0.1:{port}/x.parquet'",
        "url_json": f"SELECT * FROM read_json('https://127.0.0.1:{port}/x.json')",
        "s3": "SELECT * FROM read_parquet('s3://bucket/key.parquet')",
        "prepare_execute": f"PREPARE q AS SELECT * FROM read_text('{p}'); EXECUTE q",
        "multi_statement": f"SELECT 1; SELECT * FROM read_text('{p}')",
        "multi_statement_ddl": f"SELECT 1; CREATE TABLE smuggled AS SELECT * FROM read_text('{p}')",
        "create_macro": f"CREATE MACRO m() AS TABLE SELECT * FROM read_text('{p}')",
        "create_function": "CREATE FUNCTION f(x) AS x + 1",
        "create_view": f"CREATE VIEW v AS SELECT * FROM read_text('{p}')",
        "create_temp_view": "CREATE TEMP VIEW v AS SELECT 1",
        "create_table": "CREATE TABLE t AS SELECT 1",
        "create_table_as_file": f"CREATE TABLE t AS SELECT * FROM read_text('{p}')",
        "insert": "INSERT INTO events VALUES ('x','x','x','x',1,'{}')",
        "update": "UPDATE events SET verdict='ALLOW'",
        "delete": "DELETE FROM events",
        "drop": "DROP TABLE events",
        "alter": "ALTER TABLE events ADD COLUMN z INT",
        "truncate": "TRUNCATE events",
        "create_secret": f"CREATE SECRET s (TYPE S3, KEY_ID 'a', SECRET 'b')",
        "create_sequence": "CREATE SEQUENCE s",
        "call": "CALL pragma_database_list()",
        "explain": "EXPLAIN SELECT 1",
        "describe": "DESCRIBE events",
        "summarize": "SUMMARIZE events",
        "show": "SHOW TABLES",
        "set_variable": "SET VARIABLE a = 1",
        "checkpoint": "CHECKPOINT",
        "use": "USE memory",
        "vacuum": "VACUUM",
        "query_fn": f"SELECT * FROM query('SELECT * FROM read_text(''{p}'')')",
        "query_table_fn": "SELECT * FROM query_table('events')",
        "from_other_catalog": "SELECT * FROM memory.main.events",
        "from_system": "SELECT * FROM system.main.duckdb_settings()",
        "sniff": f"SELECT * FROM sniff_csv('{p}')",
        "parquet_metadata": f"SELECT * FROM parquet_metadata('{p}')",
        "excel": f"SELECT * FROM read_xlsx('{p}')",
        "sqlite_scan": f"SELECT * FROM sqlite_scan('{p}', 't')",
        "postgres_scan": "SELECT * FROM postgres_scan('host=127.0.0.1', 'public', 't')",
        "lateral_func": f"SELECT t.* FROM events e, LATERAL (SELECT * FROM read_text('{p}')) t",
        "cte_func": f"WITH c AS (SELECT * FROM read_text('{p}')) SELECT * FROM c",
        "subquery_func": f"SELECT (SELECT content FROM read_text('{p}'))",
        "case_mixed": f"SeLeCt * FrOm ReAd_TeXt('{p}')",
        "quoted_ident": f"SELECT * FROM \"read_text\"('{p}')",
        "comment_smuggle": f"SELECT 1 /* */ ; /* */ COPY (SELECT 1) TO '{o}'",
        "unicode_space": f"SELECT * FROM read_text('{p}')",
        "set_op": f"SELECT content FROM read_text('{p}') UNION ALL SELECT 'x'",
        "pivot": f"PIVOT events ON verdict USING sum(amount)",
        "summary_fn": f"SELECT * FROM duckdb_secrets()",
        "write_log": "SELECT write_log('x')",
        "sleep_hint": "SELECT current_database(), current_schema()",
    }


@pytest.fixture()
def host(tmp_path):
    """A canary file, a would-be output path/dir, a loopback server that must never be contacted."""
    d = tmp_path / "host"
    d.mkdir()
    canary = d / "secret.txt"
    canary.write_text(CANARY + "\n")
    for ext in (".csv",):
        (d / ("secret.txt" + ext)).write_text(f"a\n{CANARY}\n")
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            hits.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(CANARY.encode())
        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield {"dir": d, "canary": str(canary), "out": str(d / "OUT.csv"), "outdir": str(d / "export_dir"),
               "port": srv.server_address[1], "hits": hits}
    finally:
        srv.shutdown()


def _assert_no_leak(host, text):
    assert CANARY not in text
    assert str(host["dir"]) not in text            # a refusal never echoes the caller's host path back
    assert "BETA-ONLY-MARKER" not in text


def _after_state(host):
    assert not os.path.exists(host["out"]) and not os.path.exists(host["out"] + ".db")
    assert not os.path.exists(host["outdir"])
    assert host["hits"] == []                   # the worker never reached the network


def test_every_vector_is_refused_by_the_service(tmp_path, host):
    svc, sink = make_service(tmp_path)
    vec = _vectors(host["canary"], host["out"], host["outdir"], host["port"])
    for name, sql in vec.items():
        try:
            r = svc.query(owner(), "alpha", sql)
        except QueryRefused as e:
            _assert_no_leak(host, str(e))
            continue
        # permitted harmless forms: none of the vectors is meant to succeed except pure scalar probes
        assert name in {"sleep_hint"} and CANARY not in repr(r.rows), name
    _after_state(host)
    assert all(r["outcome"] in ("refused", "error", "ok") for r in sink.receipts)


def test_every_vector_fails_in_the_sandbox_even_with_the_guard_disabled(tmp_path, host):
    """The boundary claim: with the statement guard OFF the worker still cannot touch the host."""
    cat = make_catalog()
    data = cat.load_scope("alpha")
    vec = _vectors(host["canary"], host["out"], host["outdir"], host["port"])
    leaked = {}
    for name, sql in vec.items():
        try:
            res = W.execute(data, sql, Limits(timeout_s=20), _unsafe_disable_guard=True)
            text = repr(res.rows) + repr(res.columns)
        except W.WorkerError as e:
            text = str(e)
        if CANARY in text or str(host["dir"]) in text:
            leaked[name] = text[:200]
        assert "BETA" not in text, name
    assert leaked == {}, leaked
    _after_state(host)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('{p}')", "SELECT * FROM read_csv('{p}')", "SELECT * FROM glob('{d}/*')",
    "COPY (SELECT 1) TO '{o}'", "ATTACH '{o}.db'", "EXPORT DATABASE '{od}'", "INSTALL httpfs", "LOAD httpfs",
    "SET enable_external_access = true", "SET lock_configuration = false",
])
def test_unguarded_failure_is_a_permission_failure_not_a_missing_feature(tmp_path, host, sql):
    data = make_catalog().load_scope("alpha")
    with pytest.raises(W.WorkerError) as e:
        W.execute(data, sql.format(p=host["canary"], d=host["dir"], o=host["out"], od=host["outdir"]),
                  Limits(timeout_s=20), _unsafe_disable_guard=True)
    assert CANARY not in str(e.value) and str(host["dir"]) not in str(e.value)


def test_duckdb_layer_alone_blocks_the_host_with_guard_and_os_sandbox_both_off(tmp_path, host):
    """Layer independence: DuckDB external-access-off + locked configuration hold even with NO guard and NO OS sandbox."""
    data = make_catalog().load_scope("alpha")
    for name in ("read_text", "read_csv", "read_parquet", "read_json", "read_blob", "glob", "replacement_scan", "copy_to",
                 "copy_from", "export_database", "attach", "install_httpfs", "load_httpfs", "set_external", "set_lock",
                 "url_csv", "create_secret", "prepare_execute"):
        sql = _vectors(host["canary"], host["out"], host["outdir"], host["port"])[name]
        try:
            res = W.execute(data, sql, Limits(timeout_s=20), os_sandbox="off", _unsafe_disable_guard=True)
            text = repr(res.rows)
        except W.WorkerError as e:
            text = str(e)
        assert CANARY not in text and str(host["dir"]) not in text, name
    _after_state(host)


def test_configuration_is_locked_and_external_access_off_in_the_worker():
    s = W.selftest("settings")
    assert s["enable_external_access"] is False and s["lock_configuration"] is True
    assert s["autoinstall_known_extensions"] is False and s["autoload_known_extensions"] is False
    assert s["allow_community_extensions"] is False and s["allow_unsigned_extensions"] is False
    assert s["duckdb_version"] == "1.5.5" or s["duckdb_version"].startswith("1.")
    assert s["threads"] == 1
    import re as _re
    num, unit = _re.match(r"([\d.]+)\s*(\w+)", s["memory_limit"]).groups()
    assert unit in ("MiB", "MB") and float(num) <= 300


# ───────────────────────── statement guard (parser based) ─────────────────────────
GOOD = ["SELECT 1", "WITH a AS (SELECT 1 x) SELECT * FROM a", "SELECT * FROM events e JOIN events f USING (mission_id)",
        "SELECT sum(amount) OVER (PARTITION BY tenant) FROM events", "SELECT * FROM range(3)", "SELECT unnest([1,2])",
        "SELECT * FROM generate_series(1,3)", "SELECT json_extract(payload,'$.k') FROM events", "VALUES (1),(2)",
        "SELECT * FROM events UNION ALL SELECT * FROM events", "SELECT * FROM denials",
        "SELECT * FROM (SELECT * FROM events) s", "SELECT * FROM events WHERE amount IN (SELECT amount FROM events)"]


@pytest.mark.parametrize("sql", GOOD)
def test_guard_accepts_read_only_expressive_selects(sql):
    check_statement(sql, allowed_tables={"events", "denials"})


@pytest.mark.parametrize("sql", ["", "   ", "SELECT 1; SELECT 2", "INSERT INTO events VALUES (1)", "DROP TABLE events",
                                 "SELECT * FROM read_text('x')", "SELECT * FROM other_table", "SELECT * FROM main.events",
                                 "SELECT * FROM memory.main.events", "SELECT * FROM information_schema.tables",
                                 "SELECT * FROM duckdb_settings()", "SELECT current_setting('x')", "SELECT getenv('A')",
                                 "SELECT * FROM 'file.csv'", "PRAGMA x", "SET a=1", "COPY events TO 'x'", "ATTACH 'x'",
                                 "SELECT \x00 1", "SELECT * FROM events -- ;\n; DROP TABLE events", "EXPLAIN SELECT 1",
                                 "SELECT * FROM query('select 1')", "SELECT 1" + " " * 20000])
def test_guard_rejects(sql):
    with pytest.raises(StatementRejected):
        check_statement(sql, allowed_tables={"events", "denials"})


def test_guard_error_messages_are_fixed_and_do_not_echo_paths():
    with pytest.raises(StatementRejected) as e:
        check_statement("SELECT * FROM read_text('/etc/passwd')", allowed_tables={"events"})
    assert "/etc/passwd" not in str(e.value)


# ───────────────────────── cross-scope isolation ─────────────────────────
def test_scopes_are_isolated_across_sql_introspection_errors(tmp_path):
    svc, _ = make_service(tmp_path, grants=(("owner", "evidence.alpha.inspect"), ("owner", "evidence.beta.inspect")))
    a = svc.query(owner(), "alpha", "SELECT tenant, count(*) FROM events GROUP BY 1")
    b = svc.query(owner(), "beta", "SELECT tenant, count(*) FROM events GROUP BY 1")
    assert a.rows == [["alpha", 3]] and b.rows == [["beta", 2]]
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT * FROM beta_only")
    assert "BETA" not in str(e.value)
    for sql in ("SELECT * FROM beta.events", "SELECT * FROM beta_only", "SELECT * FROM information_schema.tables"):
        with pytest.raises(QueryRefused) as e:
            svc.query(owner(), "alpha", sql)
        assert "BETA" not in str(e.value)


def test_unguarded_alpha_worker_holds_no_beta_data_or_names():
    cat = make_catalog()
    alpha = cat.load_scope("alpha")
    for sql in ("SELECT table_name FROM duckdb_tables()", "SELECT table_name FROM information_schema.tables",
                "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal", "SELECT * FROM duckdb_columns() WHERE NOT internal",
                "SELECT name, value FROM duckdb_settings()", "SELECT database_name, path FROM duckdb_databases()"):
        res = W.execute(alpha, sql, Limits(timeout_s=20), _unsafe_disable_guard=True)
        text = repr(res.rows)
        assert "beta" not in text.lower() and "BETA" not in text
    names = {r[0] for r in W.execute(alpha, "SELECT table_name FROM duckdb_tables()", Limits(timeout_s=20), _unsafe_disable_guard=True).rows}
    assert names == {"events"}


def test_each_query_runs_in_its_own_worker_process(tmp_path):
    pids = {W.selftest("pid")["pid"] for _ in range(3)}
    assert len(pids) == 3 and os.getpid() not in pids


def test_authorization_precedes_data_resolution(tmp_path):
    """A denied caller must not cause the scope's data to be loaded or a worker to be started."""
    cat = make_catalog()
    loads = []
    orig = cat.load_scope
    cat.load_scope = lambda s: (loads.append(s), orig(s))[1]
    svc, _ = make_service(tmp_path, catalog=cat)
    with pytest.raises(QueryRefused):
        svc.query(stranger(), "alpha", "SELECT 1")
    with pytest.raises(QueryRefused):
        svc.query(owner(), "beta", "SELECT 1")
    assert loads == []
    svc.query(owner(), "alpha", "SELECT 1")
    assert loads == ["alpha"]                    # only the authorized scope's data is ever built


# ───────────────────────── worker isolation ─────────────────────────
def test_worker_has_no_inherited_environment_or_cwd_state(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "s3cr3t-value")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-TEST")
    s = W.selftest("env")
    assert s["env"] == {} or set(s["env"]) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert "s3cr3t" not in repr(s) and "AKIA" not in repr(s)
    c = W.selftest("cwd")
    assert c["listing"] == [] and c["is_private"] is True
    assert not os.path.exists(c["cwd"])              # the private scratch dir is destroyed afterwards


def test_worker_resource_limits_are_applied():
    r = W.selftest("rlimits")
    assert r["fsize"] == 0 and r["core"] == 0 and r["nofile"] == 256 and 0 < r["cpu"] <= 30


def test_rss_watchdog_kills_a_runaway_worker():
    with pytest.raises(W.WorkerError) as e:
        W.selftest("alloc", mb=1500, memory_mb=64)            # watchdog cap = 2*64+200 MB
    assert e.value.code in ("limit", "error")


def test_worker_cannot_open_sockets_python_level():
    s = W.selftest("socket")
    assert s["socket_created"] is False


def test_worker_cannot_reach_a_loopback_listener_at_the_os_level():
    st = W.sandbox_status()
    if not st["network_blocked"]:
        pytest.skip(f"no OS network sandbox on this host ({st['backend']}); python-level and DuckDB-level blocks are tested separately")
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); srv.settimeout(0.3)
    try:
        s = W.selftest("os_network", port=srv.getsockname()[1])
        assert s["connected"] is False
        with pytest.raises(socket.timeout):
            srv.accept()
    finally:
        srv.close()


def test_os_sandbox_blocks_host_reads_and_writes(host):
    st = W.sandbox_status()
    s = W.selftest("os_files", read=host["canary"], write=host["out"])
    assert s["write_ok"] is False and not os.path.exists(host["out"])        # rlimit + (where available) sandbox
    if not st["file_read_blocked"]:
        pytest.skip(f"no OS file sandbox on this host ({st['backend']})")
    assert s["read_ok"] is False


def test_required_sandbox_fails_closed_when_unavailable(monkeypatch):
    monkeypatch.setattr(W, "_detect_backend", lambda: None)
    with pytest.raises(W.WorkerError):
        W.execute(make_catalog().load_scope("alpha"), "SELECT 1", Limits(timeout_s=10), os_sandbox="required")
    assert W.execute(make_catalog().load_scope("alpha"), "SELECT 1", Limits(timeout_s=10), os_sandbox="off").rows == [[1]]


# ───────────────────────── resource limits ─────────────────────────
def test_timeout_kills_the_worker(tmp_path):
    svc, sink = make_service(tmp_path, limits=Limits(timeout_s=2))
    import time
    t = time.time()
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT count(*) FROM range(1000000000) a(i) WHERE sha256(a.i::VARCHAR) LIKE 'ffffff%'")
    assert e.value.code == "timeout" and time.time() - t < 15
    assert sink.receipts[-1]["outcome"] == "timeout"
    assert W.live_workers() == 0


def test_memory_limit(tmp_path):
    svc, _ = make_service(tmp_path, limits=Limits(timeout_s=30, memory_mb=64))
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT length(string_agg(repeat('x', 1000), ',')) FROM range(5000000)")
    assert e.value.code in ("limit", "error", "timeout")
    assert W.live_workers() == 0


def test_huge_cross_join_is_bounded(tmp_path):
    svc, _ = make_service(tmp_path, limits=Limits(timeout_s=3, memory_mb=64))
    with pytest.raises(QueryRefused):
        svc.query(owner(), "alpha", "SELECT a.i, b.i, c.i FROM range(100000) a(i), range(100000) b(i), range(100000) c(i) ORDER BY 1,2,3")


def test_row_cap_truncates_and_reports(tmp_path):
    svc, _ = make_service(tmp_path, limits=Limits(timeout_s=20, max_rows=100))
    r = svc.query(owner(), "alpha", "SELECT i FROM range(1000000) t(i)")
    assert len(r.rows) == 100 and r.truncated is True


def test_result_bytes_cap(tmp_path):
    svc, _ = make_service(tmp_path, limits=Limits(timeout_s=20, max_result_bytes=2000))
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT repeat('x', 100000) FROM range(10)")
    assert e.value.code == "limit"


def test_sql_length_and_input_row_caps(tmp_path):
    svc, _ = make_service(tmp_path, limits=Limits(max_sql_chars=50))
    with pytest.raises(QueryRefused):
        svc.query(owner(), "alpha", "SELECT " + ", ".join(str(i) for i in range(100)))
    cat = ScopeCatalog()
    cat.register_scope("big", [DatasetSpec("t", (("x", "BIGINT"),), rows=[{"x": i} for i in range(50)])])
    from agentic_os.evidence_query import EvidenceQueryService, MemoryAuditSink
    from tests.evidence_query_support import make_ceiling
    s2 = EvidenceQueryService(cat, make_ceiling(tmp_path / "z", [("owner", "evidence.big")]), MemoryAuditSink(), limits=Limits(max_input_rows=10))
    with pytest.raises(QueryRefused) as e:
        s2.query(owner(), "big", "SELECT 1")
    assert e.value.code == "limit"


# ───────────────────────── audit receipts ─────────────────────────
def test_every_query_is_audited_with_who_scope_hash_rows_outcome(tmp_path):
    import hashlib
    svc, sink = make_service(tmp_path)
    sql = "SELECT * FROM events"
    svc.query(owner(), "alpha", sql)
    with pytest.raises(QueryRefused):
        svc.query(owner(), "alpha", "SELECT * FROM read_text('/etc/hosts')")
    with pytest.raises(QueryRefused):
        svc.query(stranger(), "alpha", sql)
    ok, bad, denied = sink.receipts
    assert ok["principal"] == "owner" and ok["scope"] == "alpha" and ok["rows"] == 3 and ok["outcome"] == "ok"
    assert ok["statement_sha256"] == hashlib.sha256(sql.encode()).hexdigest()
    assert ok["datasets"]["events"]                              # which evidence answered (content hash)
    assert bad["outcome"] == "refused" and bad["reason"] == "statement_rejected" and bad["rows"] == 0
    assert denied["principal"] == "stranger" and denied["reason"] == "not_authorized"
    assert "/etc/hosts" not in repr(sink.receipts)               # the receipt carries a hash, not the statement


def test_audit_log_is_hash_chained_and_tamper_evident(tmp_path):
    from agentic_os.evidence_query import JsonlAuditSink
    p = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(p))
    svc, _ = make_service(tmp_path / "x", )
    svc.audit = sink
    for _ in range(3):
        svc.query(owner(), "alpha", "SELECT 1")
    assert sink.verify() == (True, 3)
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"rows": 1', '"rows": 7')
    p.write_text("\n".join(lines) + "\n")
    assert sink.verify()[0] is False


def test_audit_failure_fails_the_query_closed(tmp_path):
    svc, _ = make_service(tmp_path)
    class Dead:
        def append(self, receipt): raise OSError("disk full")
    svc.audit = Dead()
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT 1")
    assert e.value.code == "audit_unavailable"
