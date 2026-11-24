"""Safe writer for generated code.

Generated files only ever land inside one workspace folder. A bad file is skipped and
reported; it never blocks the other files and it is never written outside the folder.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ALLOWED_EXTENSIONS = {
    ".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml",
    ".ini", ".cfg", ".sql", ".html", ".css", ".js",
}
ALLOWED_NAMES = {"Dockerfile", ".gitignore", ".env.example"}
MAX_FILES = 50
MAX_FILE_BYTES = 200_000
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@dataclass
class WriteReport:
    written: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


def validate_relative_path(path: str) -> str | None:
    """Return a reason if the path is unsafe or not allowed, else None."""
    if not isinstance(path, str) or not path.strip():
        return "path is empty"
    p = path.strip().replace("\\", "/")
    if "\x00" in p:
        return "path contains a null byte"
    if p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        return "path must be relative (no absolute paths or drive letters)"
    parts = p.split("/")
    if ".." in parts:
        return "path must not contain '..'"
    if any(part in ("", ".") for part in parts):
        return "path has an empty or '.' segment"
    for part in parts:
        if ":" in part:
            return "path must not contain ':'"
        if part != part.strip() or part.endswith("."):
            return f"'{part}' is not a valid file or folder name"
        if part.split(".")[0].lower() in _RESERVED:
            return f"'{part}' is a reserved name"
    name = parts[-1]
    if name not in ALLOWED_NAMES and PurePosixPath(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        return f"file type of '{name}' is not allowed"
    return None


def write_files(root: Path | str, files: list[tuple[str, str]]) -> WriteReport:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    report = WriteReport()
    seen: set[str] = set()

    for index, (path, content) in enumerate(files):
        error = None
        if index >= MAX_FILES:
            error = f"more than {MAX_FILES} files"
        if error is None:
            error = validate_relative_path(path)
        if error is None and not (isinstance(content, str) and content.strip()):
            error = "file content is empty"
        if error is None and len(content.encode("utf-8")) > MAX_FILE_BYTES:
            error = "file is too large"
        if error is None:
            rel = path.strip().replace("\\", "/")
            if rel in seen:
                error = "duplicate path"
            else:
                target = (root_resolved / rel).resolve()
                try:
                    target.relative_to(root_resolved)
                except ValueError:
                    error = "path escapes the workspace folder"
        if error:
            report.skipped.append((str(path), error))
            continue
        seen.add(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        report.written.append(target.relative_to(root_resolved).as_posix())
    return report
