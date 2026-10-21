from dip.extract import diff


def test_diff_reports_changed_added_and_missing_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "data").mkdir(parents=True)
        (root / "data" / "same.csv").write_bytes(b"id\n1\n")
    (a / "data" / "changed.csv").write_bytes(b"id\n1\n")
    (b / "data" / "changed.csv").write_bytes(b"id\n2\n")
    (a / "only_a.json").write_bytes(b"{}")
    (b / "only_b.json").write_bytes(b"{}")
    assert diff(a, b) == ["data/changed.csv", "only_a.json", "only_b.json"]
    assert diff(a, a) == []
