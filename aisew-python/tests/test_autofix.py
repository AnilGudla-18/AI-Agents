import ast

from aisew.agents.autofix import KNOWN_IMPORTS, apply_autofix

MODELS = ("from sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n\n\n"
          "class Price(Base):\n    __tablename__ = 'prices'\n    amount = Float\n")
TEST = "from app.models import Price\n\n\ndef test_p():\n    assert Price\n"


def nameerror(name, where="app/models.py:8"):
    return f"{where}: in <module>\nE   NameError: name '{name}' is not defined\n1 failed"


def fix(files, output):
    new, notes = apply_autofix(files, output)
    for path, content in new:  # a fix must never break the file
        if path.endswith(".py"):
            ast.parse(content)
    return dict(new), notes


def test_a_missing_sqlalchemy_import_is_added_before_the_first_import():
    files, notes = fix([("app/models.py", MODELS), ("tests/test_p.py", TEST)], nameerror("Float"))
    assert files["app/models.py"].splitlines()[0] == "from sqlalchemy import Float"
    assert "from sqlalchemy.orm import DeclarativeBase" in files["app/models.py"]
    assert files["tests/test_p.py"] == TEST  # a file that does not use the name is untouched
    assert notes == ["app/models.py: from sqlalchemy import Float"]


def test_known_names_from_other_libraries():
    cases = {"relationship": "from sqlalchemy.orm import relationship", "FastAPI": "from fastapi import FastAPI",
             "BaseModel": "from pydantic import BaseModel", "timedelta": "from datetime import timedelta",
             "Optional": "from typing import Optional", "Decimal": "from decimal import Decimal",
             "uuid": "import uuid", "pytest": "import pytest", "func": "from sqlalchemy import func"}
    for name, statement in cases.items():
        files, notes = fix([("app/a.py", f"x = {name}\n")], nameerror(name, "app/a.py:1"))
        assert files["app/a.py"].splitlines()[0] == statement, name
        assert KNOWN_IMPORTS[name] == statement


def test_the_import_goes_after_a_docstring_and_future_imports():
    text = '"""Models."""\nfrom __future__ import annotations\nx = Float\n'
    files, _ = fix([("app/a.py", text)], nameerror("Float", "app/a.py:3"))
    assert files["app/a.py"].splitlines() == ['"""Models."""', "from __future__ import annotations",
                                              "from sqlalchemy import Float", "x = Float"]
    text = '"""Models."""\nimport os\nx = Float\n'
    files, _ = fix([("app/a.py", text)], nameerror("Float", "app/a.py:3"))
    assert files["app/a.py"].splitlines()[:3] == ['"""Models."""', "from sqlalchemy import Float", "import os"]


def test_nothing_changes_when_the_name_is_already_imported_or_defined():
    for text in ("from sqlalchemy import Float\nx = Float\n", "from sqlalchemy import (\n    Column,\n    Float,\n)\nx = Float\n",
                 "import sqlalchemy as sa\nclass Float:\n    pass\nx = Float\n", "Float = 1\nx = Float\n"):
        files, notes = fix([("app/a.py", text)], nameerror("Float", "app/a.py:1"))
        assert notes == [] and files["app/a.py"] == text   # so the loop can never spin on it


def test_unknown_and_ambiguous_names_are_left_to_the_model():
    for name in ("Mystery", "Enum", "Session2"):
        files, notes = fix([("app/a.py", f"x = {name}\n")], nameerror(name, "app/a.py:1"))
        assert notes == [] and files["app/a.py"] == f"x = {name}\n"


def test_the_deepest_traceback_file_is_preferred():
    both = [("app/a.py", "x = Float\n"), ("app/b.py", "y = Float\n")]
    out = "app/a.py:1: in <module>\napp/b.py:1: in <module>\nE   NameError: name 'Float' is not defined"
    files, notes = fix(both, out)
    assert "from sqlalchemy import Float" in files["app/b.py"] and "from sqlalchemy" not in files["app/a.py"]
    assert notes == ["app/b.py: from sqlalchemy import Float"]


def test_without_a_traceback_every_file_that_uses_the_name_is_fixed():
    both = [("app/a.py", "x = Float\n"), ("app/b.py", "y = Float\n"), ("app/c.py", "z = 1\n")]
    files, notes = fix(both, "E   NameError: name 'Float' is not defined")
    assert len(notes) == 2 and files["app/c.py"] == "z = 1\n"


def test_several_missing_names_in_one_output():
    text = "x = Float\ny = relationship\n"
    out = "E   NameError: name 'Float' is not defined\nE   NameError: name 'relationship' is not defined"
    files, notes = fix([("app/a.py", text)], out)
    assert "from sqlalchemy import Float" in files["app/a.py"] and "from sqlalchemy.orm import relationship" in files["app/a.py"]
    assert len(notes) == 2


def test_a_file_with_a_syntax_error_is_not_touched():
    broken = "x = Float(\n"
    files, notes = apply_autofix([("app/a.py", broken)], nameerror("Float", "app/a.py:1"))
    assert dict(files)["app/a.py"] == broken and notes == []


def test_non_python_files_are_never_changed():
    files, notes = fix([("requirements.txt", "Float\n"), ("app/a.py", "x = Float\n")], nameerror("Float", "app/a.py:1"))
    assert files["requirements.txt"] == "Float\n" and len(notes) == 1


def test_misspelled_column_arguments_are_renamed():
    text = "from sqlalchemy import Column, Integer\nid = Column(Integer, PrimaryKey=True, Nullable=False)\n"
    out = ("app/models.py:3: in <module>\nE   TypeError: 'PrimaryKey' is an invalid keyword argument for Column()\n"
           "E   TypeError: 'Nullable' is an invalid keyword argument for Column()")
    files, notes = fix([("app/models.py", text)], out)
    assert "Column(Integer, primary_key=True, nullable=False)" in files["app/models.py"]
    assert notes == ["app/models.py: PrimaryKey= -> primary_key=", "app/models.py: Nullable= -> nullable="]


def test_unrelated_errors_change_nothing():
    files = [("app/a.py", "x = Float\n")]
    new, notes = apply_autofix(files, "E   AssertionError: assert 1 == 2\nE   ValueError: boom")
    assert new == files and notes == []


def test_the_fix_is_idempotent():
    once, _ = fix([("app/a.py", "x = Float\n")], nameerror("Float", "app/a.py:1"))
    twice, notes = fix(list(once.items()), nameerror("Float", "app/a.py:1"))
    assert twice == once and notes == []


def test_each_nameerror_is_fixed_in_its_own_failing_file():
    files = [("app/a.py", "x = Float\n"), ("app/b.py", "y = relationship\n")]
    out = ("app/a.py:1: in <module>\nE   NameError: name 'Float' is not defined\n"
           "app/b.py:1: in <module>\nE   NameError: name 'relationship' is not defined")
    fixed, notes = fix(files, out)
    assert fixed["app/a.py"].startswith("from sqlalchemy import Float")
    assert fixed["app/b.py"].startswith("from sqlalchemy.orm import relationship")
    assert "relationship" not in fixed["app/a.py"] and "Float" not in fixed["app/b.py"]
    assert len(notes) == 2


# ---------------- files that disagree about a name: "cannot import name 'X' from 'app.mod'" ----------------
EMAIL = "def send_reset_email(to, link):\n    return True\n\n\ndef verify_token(token):\n    return True\n"
IMPORTERS = "from app.email_service import send_email\n\n\ndef request_reset(email):\n    return send_email(email, 'x')\n"


def importerror(name, module="app.email_service", where="app/service.py:1"):
    return (f"{where}: in <module>\nE   ImportError: cannot import name '{name}' from '{module}' "
            f"(/work/{module.replace('.', '/')}.py)\n1 failed")


def test_a_similarly_named_function_gets_an_alias():
    files = [("app/email_service.py", EMAIL), ("app/service.py", IMPORTERS)]
    fixed, notes = fix(files, importerror("send_email"))
    assert fixed["app/email_service.py"].endswith("\n\nsend_email = send_reset_email\n")
    assert fixed["app/service.py"] == IMPORTERS            # the importing file is left alone
    assert notes == ["app/email_service.py: send_email = send_reset_email (alias: other files import send_email)"]
    namespace: dict = {}
    exec(fixed["app/email_service.py"], namespace)         # and it really works
    assert namespace["send_email"]("a@b.c", "x") is True


def test_a_name_defined_in_another_project_file_is_re_exported():
    files = [("app/email_service.py", "x = 1\n"), ("app/mailer.py", "def send_email(to, link):\n    return True\n"),
             ("app/service.py", IMPORTERS)]
    fixed, notes = fix(files, importerror("send_email"))
    assert fixed["app/email_service.py"].endswith("\n\nfrom app.mailer import send_email\n")
    assert "re-export" in notes[0]


def test_no_re_export_when_it_would_be_circular():
    files = [("app/email_service.py", "x = 1\n"),
             ("app/mailer.py", "from app.email_service import x\n\n\ndef send_email(to, link):\n    return x\n")]
    fixed, notes = fix(files, importerror("send_email"))
    assert notes == [] and fixed["app/email_service.py"] == "x = 1\n"


def test_nothing_is_guessed_when_no_name_is_similar_or_two_are_equally_similar():
    unrelated = [("app/email_service.py", "def verify_token(t):\n    return 1\n\n\ndef make_link(u):\n    return 2\n")]
    assert fix(unrelated, importerror("send_email"))[1] == []
    tie = [("app/email_service.py", "def send_reset_email(a):\n    return 1\n\n\ndef send_welcome_email(a):\n    return 2\n")]
    assert fix(tie, importerror("send_email"))[1] == []


def test_classes_and_functions_are_not_mixed_up():
    files = [("app/m.py", "class ResetToken:\n    pass\n\n\ndef reset_token():\n    return 1\n")]
    fixed, notes = fix(files, importerror("ResetTokenModel", "app.m"))
    assert fixed["app/m.py"].endswith("ResetTokenModel = ResetToken\n")
    fixed, notes = fix(files, importerror("make_reset_token", "app.m"))
    assert fixed["app/m.py"].endswith("make_reset_token = reset_token\n")


def test_a_package_init_module_and_modules_outside_the_project():
    files = [("app/__init__.py", ""), ("app/util/__init__.py", "def build_url(x):\n    return x\n")]
    fixed, notes = fix(files, importerror("build_link_url", "app.util"))
    assert "build_link_url = build_url" in fixed["app/util/__init__.py"]
    assert apply_autofix(files, importerror("Thing", "sqlalchemy.orm"))[1] == []


def test_an_import_error_that_is_not_about_a_missing_name_changes_nothing():
    files = [("app/email_service.py", EMAIL)]
    assert apply_autofix(files, importerror("send_reset_email"))[1] == []   # it exists: some other cause
    assert apply_autofix(files, "E   ModuleNotFoundError: No module named 'app.nothing'")[1] == []


def test_the_alias_fix_is_idempotent_and_never_breaks_the_file():
    files = [("app/email_service.py", EMAIL)]
    once, _ = fix(files, importerror("send_email"))
    twice, notes = fix(list(once.items()), importerror("send_email"))
    assert twice == once and notes == []
