import pytest

from aisew.workspace.writer import MAX_FILE_BYTES, validate_relative_path, write_files


@pytest.mark.parametrize("path", [
    "app/main.py", "tests/test_x.py", "requirements.txt", "Dockerfile", ".gitignore",
    ".env.example", "app/sub/deep/models.py", "README.md", "app\\models.py",
])
def test_safe_paths_are_accepted(path):
    assert validate_relative_path(path) is None


@pytest.mark.parametrize("path,why", [
    ("", "empty"),
    ("   ", "empty"),
    ("../evil.py", "'..'"),
    ("app/../../evil.py", "'..'"),
    ("app\\..\\..\\evil.py", "'..'"),
    ("/etc/passwd.py", "relative"),
    ("\\windows\\evil.py", "relative"),
    ("C:/Windows/evil.py", "relative"),
    ("c:\\evil.py", "relative"),
    ("~/evil.py", "relative"),
    ("app//main.py", "empty"),
    ("app/./main.py", "empty"),
    ("app/", "empty"),
    ("app/main.py\x00.txt", "null"),
    ("app/run.exe", "not allowed"),
    ("app/run.bat", "not allowed"),
    ("app/run.ps1", "not allowed"),
    ("noextension", "not allowed"),
    ("app/nul.py", "reserved"),
    ("app/CON.txt", "reserved"),
    ("app/file.py:stream", "':'"),
    ("app/trailingdot.", "valid"),
])
def test_unsafe_paths_are_rejected(path, why):
    reason = validate_relative_path(path)
    assert reason is not None and why in reason


def test_files_are_written_with_nested_folders(tmp_path):
    report = write_files(tmp_path / "ws", [("app/a/b.py", "x = 1"), ("README.md", "hi\n\n\n")])
    assert report.written == ["app/a/b.py", "README.md"] and report.skipped == []
    assert (tmp_path / "ws" / "app" / "a" / "b.py").read_text() == "x = 1\n"
    assert (tmp_path / "ws" / "README.md").read_text() == "hi\n"


def test_bad_files_are_skipped_without_blocking_the_rest(tmp_path):
    files = [
        ("../escape.py", "x = 1"),
        ("good.py", "x = 1"),
        ("empty.py", "   "),
        ("big.py", "x" * (MAX_FILE_BYTES + 1)),
        ("good.py", "x = 2"),
        ("also_good.py", "y = 1"),
    ]
    report = write_files(tmp_path / "ws", files)
    assert report.written == ["good.py", "also_good.py"]
    reasons = dict(report.skipped)
    assert "'..'" in reasons["../escape.py"]
    assert "empty" in reasons["empty.py"]
    assert "too large" in reasons["big.py"]
    assert not (tmp_path / "escape.py").exists()
    assert (tmp_path / "ws" / "good.py").read_text() == "x = 1\n"  # the duplicate did not overwrite


def test_a_symlink_cannot_be_used_to_escape(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this system")
    report = write_files(root, [("link/evil.py", "x = 1")])
    assert report.written == [] and "escapes" in report.skipped[0][1]
    assert not (outside / "evil.py").exists()


def test_too_many_files_are_skipped(tmp_path):
    files = [(f"f{i}.py", "x = 1") for i in range(55)]
    report = write_files(tmp_path / "ws", files)
    assert len(report.written) == 50 and len(report.skipped) == 5
