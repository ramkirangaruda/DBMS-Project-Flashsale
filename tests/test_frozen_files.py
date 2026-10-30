"""
The course submission is frozen. This test fails if any of it changes.

WHAT IS FROZEN, AND WHY
    app/models.py, app/database.py and the three checkout handlers in
    app/main.py (checkout_pessimistic, checkout_optimistic, checkout_redis)
    were submitted for the DBMS course and must stay byte-identical. Every
    later feature (the futility-aware admission work included) is additive
    and lives in other modules behind flags that default to off. This test
    is what makes "byte-identical" checked rather than promised.

HOW
    Whole-file SHA-256 for models.py and database.py. For main.py only the
    three handlers are pinned -- from the first decorator line to the last
    line of the body, located with ast -- so the rest of main.py may still
    change without tripping this.

    Line endings are normalised to LF before hashing. A Windows checkout with
    core.autocrlf=true has CRLF on disk while the committed blob is LF, and
    that is not a change to the submission.

    Pinned at commit 20c1080. If a frozen file ever HAS to change, that is a
    decision for the project owner, not something to fix by updating a hash.
"""
import ast
import hashlib
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FROZEN_FILES = {
    "app/models.py": "6fcc61416d282ea679a69d28acfb77328d10fd344f3520af5a027e82be480db4",
    "app/database.py": "3fac3c9b461f246bd73cd5af5db1fca301bf7bfe6a6f6e96441eb9ee8e228816",
}

FROZEN_HANDLERS = {
    "checkout_pessimistic": "c034e290163549c3976efe7110e21f2131e3376077a6345ed90cf16e62c548bb",
    "checkout_optimistic": "d356452c095cd7bf6430bd34fbb6e0d5a2ff160eadf62a2e55ae8c613e67d840",
    "checkout_redis": "4a5320869ea851bf454c31902d7548d6b0d750c9c371df0df28cb9e630a98b7b",
}


def _read_lf(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _handler_sources():
    src = _read_lf("app/main.py").decode("utf-8")
    lines = src.splitlines(keepends=True)
    found = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in FROZEN_HANDLERS:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            found[node.name] = "".join(lines[start - 1:node.end_lineno]).encode("utf-8")
    return found


@pytest.mark.parametrize("rel", sorted(FROZEN_FILES))
def test_frozen_file_unchanged(rel):
    assert _sha(_read_lf(rel)) == FROZEN_FILES[rel], (
        f"{rel} is part of the frozen course submission and must stay byte-identical")


@pytest.mark.parametrize("name", sorted(FROZEN_HANDLERS))
def test_frozen_checkout_handler_unchanged(name):
    sources = _handler_sources()
    assert name in sources, f"{name} is missing from app/main.py"
    assert _sha(sources[name]) == FROZEN_HANDLERS[name], (
        f"{name} in app/main.py is part of the frozen course submission and must stay byte-identical")
