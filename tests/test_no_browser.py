"""No browser automation anywhere (03-foundations §1, 12-testing §6 definition of done).

Importing `eta_automations` and every submodule must not import Playwright, Selenium, pyppeteer,
undetected-chromedriver or requests-html, and none of them may be a declared dependency of the package.
The import check runs twice: in this test process (as the definition of done states it) and in a fresh
interpreter, so that no other test can hide or cause a browser import. Importing also needs no Tk (11 §6) and,
per 03 §2, pandas is only imported lazily by business_days.py.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import eta_automations

BROWSER_MODULES = ("playwright", "selenium", "pyppeteer", "undetected_chromedriver", "requests_html")
BROWSER_DISTRIBUTIONS = ("playwright", "selenium", "pyppeteer", "undetected-chromedriver", "requests-html")
PACKAGE_DIR = Path(eta_automations.__file__).parent


def _submodule_names() -> list[str]:
    return sorted(info.name for info in pkgutil.walk_packages(eta_automations.__path__, prefix="eta_automations."))


def _browser_modules_in(names: list[str] | set[str]) -> list[str]:
    return sorted(name for name in names if name.split(".", 1)[0] in BROWSER_MODULES)


def test_every_submodule_is_found() -> None:
    names = _submodule_names()
    for expected in ("eta_automations.cli", "eta_automations.pipeline", "eta_automations.ui",
                     "eta_automations.qbo.email", "eta_automations.vship.client", "eta_automations.carriers.msc",
                     "eta_automations.carriers.maersk", "eta_automations.carriers.cma",
                     "eta_automations.carriers.aggregator", "eta_automations.carriers.router"):
        assert expected in names


def test_importing_the_package_and_every_submodule_imports_no_browser_library() -> None:
    importlib.import_module("eta_automations")
    for name in _submodule_names():
        importlib.import_module(name)
    assert _browser_modules_in(set(sys.modules)) == []


_FRESH_INTERPRETER_CHECK = """
import importlib, json, pkgutil, sys
import eta_automations
for info in pkgutil.walk_packages(eta_automations.__path__, prefix="eta_automations."):
    importlib.import_module(info.name)
print(json.dumps(sorted(sys.modules)))
"""


def test_fresh_interpreter_import_loads_no_browser_library_tk_or_pandas(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-c", _FRESH_INTERPRETER_CHECK], capture_output=True, text=True,
                            cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert "eta_automations.cli" in loaded and "eta_automations.pipeline" in loaded
    assert _browser_modules_in(loaded) == []
    assert "tkinter" not in loaded          # ui.pick_input_file() imports Tk lazily (11 §6)
    assert "pandas" not in loaded           # business_days imports pandas lazily (03 §2)


def test_no_browser_library_is_a_declared_dependency() -> None:
    requirements = importlib.metadata.requires("eta-automations") or []
    names = {re.split(r"[\s;<>=!~\[(]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")
             for requirement in requirements}
    assert names and not names & set(BROWSER_DISTRIBUTIONS)


def test_no_source_file_imports_a_browser_library() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                continue
            offenders.extend(f"{path.relative_to(PACKAGE_DIR)}: {m}" for m in modules
                             if m.split(".", 1)[0] in BROWSER_MODULES)
    assert offenders == []
