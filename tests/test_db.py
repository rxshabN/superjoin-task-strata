from strata import db


def test_rows_lowercase_column_names(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "names.db"))
    conn.execute("insert into metrics (key, label, claim_count) values ('revenue', 'Revenue', 1)")
    assert db.rows(conn, "select KEY, label as LABEL from metrics") == [{"key": "revenue", "label": "Revenue"}]
    assert db.one(conn, "select key from metrics where key = 'revenue'") == {"key": "revenue"}
    assert "metric_aliases" in db.tables(conn)
