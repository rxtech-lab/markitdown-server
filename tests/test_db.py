"""
Driver-level behaviour that differs between sqlite3 and libsql.

Tests run against sqlite3, so anything libsql does differently has to be
asserted explicitly here or it only shows up in production.
"""
import db


class FakeCursor:
    """Stands in for a driver cursor with a chosen fetchall() return value."""

    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchall(self):
        return self._rows


class TestRowsToDicts:
    def test_none_from_fetchall_is_an_empty_result(self):
        """
        libsql returns None from fetchall() when a statement matched no rows,
        where sqlite3 returns []. The queue hits this on every idle poll: the
        claim is an UPDATE ... RETURNING that matches nothing when there is no
        work.

        Getting this wrong is not merely a crash. The UPDATE commits before the
        result is read, so the raise happened *after* `attempts` had already
        been incremented — every poll burned a retry against a chunk no worker
        ever received, and the whole job became unclaimable within seconds.
        """
        cursor = FakeCursor(description=[("id",), ("job_id",)], rows=None)
        assert db._rows_to_dicts(cursor) == []

    def test_empty_list_from_fetchall_is_an_empty_result(self):
        cursor = FakeCursor(description=[("id",)], rows=[])
        assert db._rows_to_dicts(cursor) == []

    def test_no_description_is_an_empty_result(self):
        """A plain UPDATE with no RETURNING clause has no description."""
        cursor = FakeCursor(description=None, rows=None)
        assert db._rows_to_dicts(cursor) == []

    def test_rows_are_zipped_to_column_names(self):
        cursor = FakeCursor(
            description=[("id",), ("name",)], rows=[(1, "a"), (2, "b")]
        )
        assert db._rows_to_dicts(cursor) == [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]


class TestStaleConnection:
    """
    libsql holds a server-side stream handle that expires when idle, so a
    pooled connection dies on its own and every later statement fails with
    "Stream handle N is expired". sqlite3 has no equivalent, so this is only
    reachable in production unless asserted here.
    """

    def test_expired_stream_handle_is_treated_as_stale(self):
        exc = Exception("hrana server: Stream handle for 926860691 is expired")
        assert db._is_stale_connection(exc) is True

    def test_dropped_socket_is_treated_as_stale(self):
        assert db._is_stale_connection(Exception("Connection reset by peer")) is True
        assert db._is_stale_connection(Exception("broken pipe")) is True

    def test_ordinary_sql_errors_are_not_stale(self):
        """A retry must not paper over a real error — especially not a write,
        which could then apply twice."""
        assert db._is_stale_connection(Exception("no such table: jobs")) is False
        assert db._is_stale_connection(Exception("UNIQUE constraint failed")) is False

    def test_stale_connection_is_reopened_and_the_statement_retried(
        self, database, monkeypatch
    ):
        calls = {"n": 0}
        real_get = db.get_connection

        class Flaky:
            """Fails the first execute with a stale-handle error, as an
            expired connection would."""

            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise Exception("Stream handle for 42 is expired")
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def flaky_get():
            conn = real_get()
            return Flaky(conn) if calls["n"] == 0 else conn

        monkeypatch.setattr(db, "get_connection", flaky_get)
        assert db.query("SELECT 1 AS ok") == [{"ok": 1}]
        assert calls["n"] >= 1

    def test_a_real_error_still_propagates(self, database):
        import pytest

        with pytest.raises(Exception):
            db.query("SELECT * FROM table_that_does_not_exist")


class TestDriverSelection:
    def test_local_path_uses_sqlite(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATABASE_URL", "/tmp/x.db")
        monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", None)
        assert db.uses_libsql() is False

    def test_file_scheme_uses_sqlite(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATABASE_URL", "file:markitdown.db")
        monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", None)
        assert db.uses_libsql() is False

    def test_libsql_scheme_uses_libsql(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATABASE_URL", "libsql://db.turso.io")
        monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", None)
        assert db.uses_libsql() is True

    def test_self_hosted_http_uses_libsql(self, monkeypatch):
        """The in-cluster sqld is plain HTTP, not the libsql:// scheme."""
        import config

        monkeypatch.setattr(config, "DATABASE_URL", "http://markitdown-libsql:8080")
        monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", None)
        assert db.uses_libsql() is True

    def test_auth_token_forces_libsql(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "DATABASE_URL", "somewhere.db")
        monkeypatch.setattr(config, "DATABASE_AUTH_TOKEN", "token")
        assert db.uses_libsql() is True


class TestMigrations:
    def test_split_statements_ignores_semicolons_in_comments(self):
        """
        A ';' inside a line comment used to cut a statement in half, which is
        how "-- exclusive; -1 means the whole file" broke every migration.
        """
        sql = """
        CREATE TABLE t (
          a INTEGER,   -- exclusive; -1 means the whole file
          b TEXT
        );
        CREATE INDEX i ON t(a);
        """
        statements = db._split_statements(sql)
        assert len(statements) == 2
        assert statements[0].startswith("CREATE TABLE")
        assert statements[1].startswith("CREATE INDEX")

    def test_migrations_are_idempotent(self, database):
        """Every pod runs migrations on boot, so re-running must be a no-op."""
        db._migrated = False
        db.migrate()
        assert db.query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0
