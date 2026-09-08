from strata import db


def test_rows_lowercase_column_names(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "names.db"))
    conn.execute("insert into metrics (key, label, claim_count) values ('revenue', 'Revenue', 1)")
    assert db.rows(conn, "select KEY, label as LABEL from metrics") == [{"key": "revenue", "label": "Revenue"}]
    assert db.one(conn, "select key from metrics where key = 'revenue'") == {"key": "revenue"}
    assert "metric_aliases" in db.tables(conn)


def test_worker_connection_shares_the_sqlite_file(tmp_path, monkeypatch):
    import dataclasses

    from strata import config

    settings = dataclasses.replace(config.settings, db="sqlite", db_path=tmp_path / "shared.db")
    monkeypatch.setattr(config, "settings", settings)
    main = db.init(db.connect())
    main.execute("insert into metrics (key, claim_count) values ('revenue', 1)")
    db.commit(main)
    worker = db.connect(worker=True)
    assert db.one(worker, "select key from metrics")["key"] == "revenue"
    db.refresh(worker)
