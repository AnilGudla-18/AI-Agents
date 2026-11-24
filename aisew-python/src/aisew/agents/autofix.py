"""Fixes for mistakes that do not need an AI: a missing import, a misspelled Column argument.

Small models make these mistakes all the time, and a plain rule fixes them in milliseconds,
so they are tried before the model is asked to rewrite anything. Every fix is reported.
"""
import ast
import difflib
import re

from .developer import _files_in_traceback

_SQLALCHEMY = (
    "Column Integer BigInteger SmallInteger String Text Float Numeric Boolean Date Time DateTime Interval "
    "LargeBinary JSON ForeignKey UniqueConstraint CheckConstraint Index create_engine select insert update "
    "delete func text and_ or_ not_ desc asc event inspect MetaData Table"
).split()
_ORM = "relationship sessionmaker Session DeclarativeBase Mapped mapped_column declarative_base backref".split()
_FASTAPI = "FastAPI HTTPException Depends APIRouter Query Path Body".split()
_PYDANTIC = "BaseModel Field".split()
_DATETIME = "datetime date timedelta timezone".split()
_TYPING = "Optional List Dict Any Union Tuple Set".split()

KNOWN_IMPORTS: dict[str, str] = {}
for _n in _SQLALCHEMY:
    KNOWN_IMPORTS[_n] = f"from sqlalchemy import {_n}"
for _n in _ORM:
    KNOWN_IMPORTS[_n] = f"from sqlalchemy.orm import {_n}"
for _n in _FASTAPI:
    KNOWN_IMPORTS[_n] = f"from fastapi import {_n}"
for _n in _PYDANTIC:
    KNOWN_IMPORTS[_n] = f"from pydantic import {_n}"
for _n in _DATETIME:
    KNOWN_IMPORTS[_n] = f"from datetime import {_n}"
for _n in _TYPING:
    KNOWN_IMPORTS[_n] = f"from typing import {_n}"
KNOWN_IMPORTS["Decimal"] = "from decimal import Decimal"
for _n in ("uuid", "json", "os", "re", "math", "pytest"):
    KNOWN_IMPORTS[_n] = f"import {_n}"
# Deliberately NOT listed because the right import is ambiguous: Enum, Session (orm vs requests), Table.

KEYWORD_RENAMES = {"PrimaryKey": "primary_key", "Nullable": "nullable", "Default": "default", "Unique": "unique"}


def _bound_names(text: str) -> set[str] | None:
    """Names a file already imports or defines. None if the file does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def _insert_import(text: str, statement: str) -> str:
    """Put the import before the first existing import, after any docstring / __future__ imports."""
    tree = ast.parse(text)
    body = tree.body
    index = 0
    start = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        index, start = body[0].end_lineno, 1
    for node in body[start:]:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            index = node.end_lineno
        else:
            break
    first = next((n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))
                  and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")), None)
    if first is not None:
        index = max(index, first.lineno - 1)
    lines = text.split("\n")
    lines.insert(index, statement)
    return "\n".join(lines)


def _definitions(text: str) -> dict[str, str] | None:
    """Top-level names a file defines: name -> 'class' | 'function' | 'other'. None if it does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found[node.name] = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = "function"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = "other"
    return found


def _module_path(module: str, files: list[str]) -> str | None:
    base = module.replace(".", "/")
    return next((c for c in (f"{base}.py", f"{base}/__init__.py") if c in files), None)


def _path_module(path: str) -> str:
    return path[:-3].removesuffix("/__init__").replace("/", ".")


def _imports_module(text: str, module: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return True
        if isinstance(node, ast.Import) and any(a.name == module for a in node.names):
            return True
    return False


def _tokens(name: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)]


def _closest(name: str, definitions: dict[str, str]) -> str | None:
    """The one definition that is clearly the same thing under another name (send_email ~ send_reset_email)."""
    kind = "class" if name[:1].isupper() else "function"
    wanted = set(_tokens(name))
    scored: list[tuple[float, str]] = []
    for candidate, candidate_kind in definitions.items():
        if candidate_kind != kind or candidate.startswith("_"):
            continue
        have = set(_tokens(candidate))
        if wanted and have and (wanted <= have or have <= wanted):
            scored.append((len(wanted & have) / len(wanted | have), candidate))
        else:
            ratio = difflib.SequenceMatcher(None, name.lower(), candidate.lower()).ratio()
            if ratio >= 0.8:
                scored.append((ratio, candidate))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        return None  # two equally good guesses: do not guess
    return scored[0][1]


def _nameerror_sites(output: str, known: list[str]) -> list[tuple[str, str | None]]:
    """Each NameError with the project file of the frame just above it (where it was raised)."""
    sites: list[tuple[str, str | None]] = []
    last: str | None = None
    for line in output.splitlines():
        match = re.search(r"NameError: name '(\w+)' is not defined", line)
        if match:
            sites.append((match.group(1), last))
            continue
        found = _files_in_traceback(line, known)
        if found:
            last = found[0]
    return sites


def apply_autofix(files: list[tuple[str, str]], test_output: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Returns (files in the same order, a note for each change). No notes means nothing was changed."""
    contents = dict(files)
    notes: list[str] = []
    py_files = [p for p, _ in files if p.endswith(".py")]

    for name, site in _nameerror_sites(test_output, py_files):
        statement = KNOWN_IMPORTS.get(name)
        if not statement:
            continue
        uses = [p for p in py_files if re.search(rf"\b{re.escape(name)}\b", contents[p])]
        targets = [site] if site in uses else uses   # the file of the failing frame, else every file using the name
        for path in targets:
            bound = _bound_names(contents[path])
            if bound is None or name in bound:
                continue
            contents[path] = _insert_import(contents[path], statement)
            notes.append(f"{path}: {statement}")

    for bad in dict.fromkeys(re.findall(r"'(\w+)' is an invalid keyword argument", test_output)):
        good = KEYWORD_RENAMES.get(bad)
        if not good:
            continue
        for path in py_files:
            fixed = re.sub(rf"\b{bad}=", f"{good}=", contents[path])
            if fixed != contents[path]:
                contents[path] = fixed
                notes.append(f"{path}: {bad}= -> {good}=")

    # "cannot import name 'X' from 'app.mod'": the files disagree about a name
    for name, module in dict.fromkeys(re.findall(r"cannot import name '(\w+)' from '([\w.]+)'", test_output)):
        path = _module_path(module, py_files)
        if path is None:
            continue  # not one of our files: nothing to fix by rule
        defined = _definitions(contents[path])
        if defined is None or name in defined or name in (_bound_names(contents[path]) or set()):
            continue
        owners = [p for p in py_files if p != path and name in (_definitions(contents[p]) or {})]
        if len(owners) == 1 and not _imports_module(contents[owners[0]], module):
            line = f"from {_path_module(owners[0])} import {name}"
            note = f"{path}: {line} (re-export: {name} is defined in {owners[0]})"
        else:
            target = None if owners else _closest(name, defined)
            if target is None:
                continue
            line = f"{name} = {target}"
            note = f"{path}: {line} (alias: other files import {name})"
        contents[path] = contents[path].rstrip("\n") + f"\n\n\n{line}\n"
        notes.append(note)

    return [(p, contents[p]) for p, _ in files], notes
