"""Seeded-bug pull requests for evaluating reviewers.

Each case is a before/after pair of files. The diff is generated with difflib, and each expected
issue is located by a substring of the buggy line, so line numbers are always correct. Clean cases
contain no bugs: any comment on them counts as noise.

`needs_reasoning` marks bugs no pattern-matcher can find (logic, authorisation, concurrency). They
are what an LLM reviewer is for.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class Expected:
    path: str
    contains: str  # substring identifying the buggy line in the new file
    category: str
    needs_reasoning: bool = False
    line: int = 0  # filled in by build()


@dataclass
class Case:
    id: str
    title: str
    files: dict[str, tuple[str, str]]  # path -> (before, after); before "" = new file
    expected: list[Expected] = field(default_factory=list)
    diff: str = ""

    def build(self) -> Case:
        parts = []
        for path, (before, after) in self.files.items():
            a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
            header = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if not before else "")
            body = "".join(difflib.unified_diff(a, b, "/dev/null" if not before else f"a/{path}", f"b/{path}"))
            parts.append(header + body)
        self.diff = "".join(p if p.endswith("\n") else p + "\n" for p in parts)
        for e in self.expected:
            lines = self.files[e.path][1].splitlines()
            e.line = next(i for i, ln in enumerate(lines, 1) if e.contains in ln)
        return self


CASES = [
    Case(
        "sql-injection",
        "Add user search endpoint",
        {
            "app/users.py": (
                "",
                """from app.db import conn


def search_users(name):
    cur = conn.cursor()
    cur.execute(f"SELECT id, email FROM users WHERE name LIKE '%{name}%'")
    return cur.fetchall()
""",
            )
        },
        [Expected("app/users.py", 'cur.execute(f"SELECT', "security")],
    ),
    Case(
        "shell-injection",
        "Add thumbnail generation",
        {
            "media/thumbs.py": (
                "",
                """import subprocess


def make_thumbnail(upload_path: str, size: int = 256) -> str:
    out = upload_path + ".thumb.jpg"
    subprocess.run(f"convert {upload_path} -resize {size}x{size} {out}", shell=True, check=True)
    return out
""",
            )
        },
        [Expected("media/thumbs.py", "shell=True", "security")],
    ),
    Case(
        "hardcoded-secret",
        "Integrate payment provider",
        {
            "billing/client.py": (
                "",
                """import httpx

PAYMENTS_API_KEY = "demo-key-not-real-7f3a9c2e"


def charge(customer_id: str, amount_cents: int) -> dict:
    r = httpx.post("https://api.stripe.com/v1/charges",
                   auth=(PAYMENTS_API_KEY, ""),
                   data={"customer": customer_id, "amount": amount_cents, "currency": "inr"})
    r.raise_for_status()
    return r.json()
""",
            )
        },
        [Expected("billing/client.py", "PAYMENTS_API_KEY = ", "security")],
    ),
    Case(
        "mutable-default",
        "Collect tags while parsing",
        {
            "parser/tags.py": (
                "",
                """def extract_tags(text, found=[]):
    for word in text.split():
        if word.startswith("#"):
            found.append(word[1:].lower())
    return found
""",
            )
        },
        [Expected("parser/tags.py", "def extract_tags", "correctness")],
    ),
    Case(
        "unsafe-yaml",
        "Load pipeline config from upload",
        {
            "ci/config.py": (
                "",
                """import yaml


def load_pipeline(uploaded_text: str) -> dict:
    config = yaml.load(uploaded_text)
    if "stages" not in config:
        raise ValueError("pipeline needs stages")
    return config
""",
            )
        },
        [Expected("ci/config.py", "yaml.load(", "security")],
    ),
    Case(
        "missing-authz",
        "Allow deleting projects",
        {
            "api/projects.py": (
                """from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.db import Project, session

router = APIRouter()


@router.get("/projects/{project_id}")
def get_project(project_id: int, user=Depends(current_user)):
    project = session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(404)
    return project
""",
                """from fastapi import APIRouter, Depends, HTTPException

from api.auth import current_user
from api.db import Project, session

router = APIRouter()


@router.get("/projects/{project_id}")
def get_project(project_id: int, user=Depends(current_user)):
    project = session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(404)
    return project


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, user=Depends(current_user)):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404)
    session.delete(project)
    session.commit()
    return {"deleted": project_id}
""",
            )
        },
        [Expected("api/projects.py", "session.delete(project)", "security", needs_reasoning=True)],
    ),
    Case(
        "off-by-one",
        "Paginate results",
        {
            "api/paging.py": (
                "",
                '''def paginate(items: list, page: int, per_page: int = 20) -> dict:
    """Pages are 1-based."""
    start = page * per_page
    end = start + per_page
    return {"page": page, "items": items[start:end], "total": len(items)}
''',
            )
        },
        [Expected("api/paging.py", "start = page * per_page", "correctness", needs_reasoning=True)],
    ),
    Case(
        "none-handling",
        "Show manager name on profile",
        {
            "hr/profile.py": (
                "",
                """from hr.db import find_employee


def profile_card(employee_id: int) -> dict:
    emp = find_employee(employee_id)  # returns None if not found
    manager = find_employee(emp.manager_id)
    return {"name": emp.name, "manager": manager.name}
""",
            )
        },
        [
            Expected("hr/profile.py", "manager = find_employee(emp.manager_id)", "correctness", needs_reasoning=True),
        ],
    ),
    Case(
        "n-plus-one",
        "Order history page",
        {
            "shop/history.py": (
                "",
                """from shop.db import Order, Product, session


def order_history(user_id: int) -> list[dict]:
    orders = session.query(Order).filter_by(user_id=user_id).all()
    rows = []
    for order in orders:
        for item in order.items:
            product = session.query(Product).get(item.product_id)
            rows.append({"order": order.id, "product": product.name, "qty": item.qty})
    return rows
""",
            )
        },
        [Expected("shop/history.py", "session.query(Product).get", "performance", needs_reasoning=True)],
    ),
    Case(
        "race-condition",
        "Reserve stock on checkout",
        {
            "shop/stock.py": (
                "",
                """from shop.db import Product, session


def reserve(product_id: int, qty: int) -> bool:
    product = session.get(Product, product_id)
    if product.stock >= qty:
        product.stock = product.stock - qty
        session.commit()
        return True
    return False
""",
            )
        },
        [Expected("shop/stock.py", "product.stock = product.stock - qty", "correctness", needs_reasoning=True)],
    ),
    Case(
        "swallowed-error",
        "Retry webhook delivery",
        {
            "hooks/send.py": (
                "",
                """import httpx


def deliver(url: str, payload: dict, attempts: int = 3) -> bool:
    for _ in range(attempts):
        try:
            httpx.post(url, json=payload, timeout=5).raise_for_status()
            return True
        except:
            pass
    return False
""",
            )
        },
        [Expected("hooks/send.py", "except:", "correctness")],
    ),
    Case(
        "timezone-bug",
        "Expire trial accounts",
        {
            "accounts/trial.py": (
                "",
                '''from datetime import datetime, timedelta


def trial_expired(started_at: datetime, days: int = 14) -> bool:
    """started_at is stored as an aware UTC datetime."""
    return datetime.now() > started_at + timedelta(days=days)
''',
            )
        },
        [Expected("accounts/trial.py", "datetime.now() >", "correctness", needs_reasoning=True)],
    ),
    # Clean PRs: correct code. Every comment here is noise.
    Case(
        "clean-refactor",
        "Extract slug helper",
        {
            "utils/text.py": (
                "",
                '''import re
import unicodedata


def slugify(value: str, max_length: int = 60) -> str:
    """Lowercase ASCII slug: "Héllo, World!" -> "hello-world"."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:max_length].rstrip("-")
''',
            ),
            "tests/test_text.py": (
                "",
                """from utils.text import slugify


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Héllo, World!") == "hello-world"


def test_slugify_respects_max_length():
    assert len(slugify("a" * 100, max_length=10)) == 10
""",
            ),
        },
    ),
    Case(
        "clean-param-query",
        "Add parametrised lookup",
        {
            "app/lookup.py": (
                "",
                """from app.db import conn


def user_by_email(email: str):
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
    return cur.fetchone()
""",
            )
        },
    ),
    Case(
        "clean-config",
        "Read settings from environment",
        {
            "app/settings.py": (
                "",
                """import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///local.db")
    api_key: str = os.environ.get("API_KEY", "")
    debug: bool = os.environ.get("DEBUG", "0") == "1"
""",
            )
        },
    ),
]


def load_cases() -> list[Case]:
    return [c.build() for c in CASES]
