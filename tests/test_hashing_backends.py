from pathlib import Path

from styler import hashing


def test_python_hash_backend_matches_persisted_contract(tmp_path: Path, monkeypatch) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"styler-0.13.1\n" * 100)
    monkeypatch.setattr(hashing, "_rust", None)
    checksum, size = hashing.hash_file(str(sample))
    expected, expected_size = hashing._hash_file_python(str(sample))
    assert (checksum, size) == (expected, expected_size)
    assert len(checksum) == 32
    assert hashing.active_backend() == "python"


def test_hash_tree_ignores_symlinks(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "real.txt"
    target.write_text("hola", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(target)
    monkeypatch.setattr(hashing, "_rust", None)
    entries = hashing.hash_tree([str(tmp_path)])
    assert [Path(item.path).name for item in entries] == ["real.txt"]
