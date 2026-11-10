"""Template safety and accessibility checks on the template sources and the rendered pages."""

from __future__ import annotations

import re
from importlib import resources

import pytest
from tests.support.builders import (
    FIXED_TIME,
    make_detail,
    make_entry,
    make_input,
    make_run,
    make_step,
    make_step_record,
)
from tests.support.fakes import FakeRunControl, MemoryRunReader
from tests.unit.web.helpers import TOKEN, MakeWeb, Web, elements, project_workflow, text

from treadle.config import Settings
from treadle.errors import InputError
from treadle.models import InputType, RunStatus, StepStatus, TriggerKind, TriggerState
from treadle.types import Issue, Position, Severity

TEMPLATES = resources.files("treadle") / "web" / "templates"
STATIC = resources.files("treadle") / "web" / "static"
TEMPLATE_NAMES = sorted(item.name for item in TEMPLATES.iterdir() if item.name.endswith(".html"))
SCRIPT_NAME = "<script>alert('name')</script>"
ISSUE = Issue(Severity.WARNING, "W105", "<b>too long</b>", ("timeout",), Position(1, 1))
CONTROLS = ("input", "select", "textarea")
UNLABELLED_INPUTS = ("hidden", "submit", "button")


@pytest.fixture
def busy(make_web: MakeWeb, settings: Settings) -> Web:
    """An application with a workflow named like markup, its runs, and a signed-in user."""
    workflow = project_workflow(
        settings,
        make_step("build", name=SCRIPT_NAME),
        workflow_id="deploy",
        name=SCRIPT_NAME,
        description=SCRIPT_NAME,
        inputs=[
            make_input("note", description=SCRIPT_NAME),
            make_input("ratio", InputType.NUMBER),
            make_input("count", InputType.INTEGER, required=True),
            make_input("flag", InputType.BOOLEAN),
            make_input("mode", InputType.CHOICE, options=["a", SCRIPT_NAME]),
        ],
    )
    running = make_run(
        1,
        workflow_id="deploy",
        workflow_name=SCRIPT_NAME,
        status=RunStatus.RUNNING,
        error=SCRIPT_NAME,
        inputs={"note": SCRIPT_NAME},
        trigger={"kind": "manual", "origin": SCRIPT_NAME},
        started_at=FIXED_TIME,
    )
    finished = make_run(2, workflow_id="deploy", workflow_name=SCRIPT_NAME, status=RunStatus.FAILED)
    steps = [make_step_record("build", name=SCRIPT_NAME, status=StepStatus.RUNNING)]
    reader = MemoryRunReader(make_detail(running, steps), make_detail(finished, steps))
    trigger = TriggerState("deploy", "k", TriggerKind.WATCH, SCRIPT_NAME, None, SCRIPT_NAME)
    return make_web(
        token=TOKEN,
        workflows=[make_entry(workflow, issues=[ISSUE])],
        reader=reader,
        triggers=[trigger],
        runs=FakeRunControl(error=InputError({"count": SCRIPT_NAME, "": SCRIPT_NAME})),
    )


async def rendered_pages(web: Web) -> dict[str, str]:
    """Render one page of every template, the error and 422 pages included."""
    await web.login()
    pages = {path: (await web.client.get(path)).text for path in ("/", "/workflows/deploy")}
    for path in ("/runs", "/runs/1", "/runs/2", "/nothing-here"):
        pages[path] = (await web.client.get(path)).text
    pages["422"] = (await web.post_form("/workflows/deploy/run", [("input.count", "x")])).text
    pages["/login"] = (await web.client.get("/login")).text
    return pages


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_templates_have_no_inline_scripts_styles_or_safe_filters(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")

    for script in re.findall(r"<script\b[^>]*>", source):
        assert "src=" in script, f"{name} has an inline script"
    assert not re.search(r"<script\b[^>]*>(?!</script>)", source), f"{name} has script content"
    assert "<style" not in source
    assert not re.search(r"\sstyle\s*=", source), f"{name} has an inline style"
    assert not re.search(r"\son[a-z]+\s*=", source), f"{name} has an inline event handler"
    assert not re.search(r"\|\s*safe\b", source), f"{name} uses |safe"
    assert "autoescape false" not in source
    assert "javascript:" not in source


def test_the_package_ships_exactly_these_templates() -> None:
    assert TEMPLATE_NAMES == [
        "base.html",
        "error.html",
        "login.html",
        "run.html",
        "runs.html",
        "workflow.html",
        "workflows.html",
    ]


def test_the_script_stays_under_100_lines() -> None:
    script = (STATIC / "treadle.js").read_text(encoding="utf-8")

    assert len(script.splitlines()) < 100
    assert "innerHTML" not in script
    assert "eval(" not in script


async def test_rendered_pages_escape_markup_from_workflows_and_runs(busy: Web) -> None:
    for path, html in (await rendered_pages(busy)).items():
        assert "<script>alert" not in html, path
        assert "<b>too long" not in html, path
        assert re.findall(r"<script\b[^>]*>", html) == [
            '<script src="/static/treadle.js" defer>'
        ], path
        assert not re.search(r"\sstyle\s*=", html), path
    workflow_page = (await busy.client.get("/workflows/deploy")).text
    assert "&lt;script&gt;alert(&#39;name&#39;)&lt;/script&gt;" in workflow_page


async def test_every_table_header_has_a_scope(busy: Web) -> None:
    for path, html in (await rendered_pages(busy)).items():
        headers = elements(html, "th")
        assert all(header.get("scope") in ("col", "row") for header in headers), path


async def test_every_control_has_a_visible_label(busy: Web) -> None:
    for path, html in (await rendered_pages(busy)).items():
        labelled = {label.get("for") for label in elements(html, "label")}
        for control in (element for element in elements(html) if element.tag in CONTROLS):
            if control.get("type") in UNLABELLED_INPUTS:
                continue
            assert control.get("id") in labelled, f"{path}: {control}"


async def test_every_page_has_landmarks_and_a_skip_link(busy: Web) -> None:
    for path, html in (await rendered_pages(busy)).items():
        tags = {element.tag for element in elements(html)}
        assert {"header", "nav", "main", "footer"} <= tags, path
        first_link = elements(html, "a")[0]
        assert first_link.get("href") == "#main", path
        assert elements(html, "main", id="main"), path
        assert elements(html, "html", lang="en"), path
        assert elements(html, "nav", aria_label="Main"), path


async def test_status_is_text_with_a_hidden_icon(busy: Web) -> None:
    pages = await rendered_pages(busy)

    for path, html in pages.items():
        for badge in re.findall(r'<span class="status status-[a-z_]+">(.*?)</span></span>', html):
            assert badge.startswith('<span class="status-icon" aria-hidden="true">'), path
        icons = elements(html, "span", class_="status-icon")
        assert all(icon.get("aria-hidden") == "true" for icon in icons), path
    assert "▶ running" in text(pages["/runs/1"])
    assert "✗ failed" in text(pages["/runs/2"])


async def test_the_run_page_announces_status_changes(busy: Web) -> None:
    pages = await rendered_pages(busy)

    assert elements(pages["/runs/1"], "p", id="run-status", aria_live="polite")
    assert elements(pages["/runs/1"], "pre", id="live-log")


async def test_the_422_page_links_errors_to_their_fields(busy: Web) -> None:
    html = (await rendered_pages(busy))["422"]

    [summary] = elements(html, "div", class_="error-summary")
    assert summary.get("role") == "alert"
    assert summary.get("aria-labelledby") == "error-summary-heading"
    assert elements(html, "h3", id="error-summary-heading")
    assert elements(html, "a", href="#input-count")
    [count] = elements(html, "input", id="input-count")
    assert count.get("aria-describedby") == "input-count-error"
    [note] = elements(html, "input", id="input-note")
    assert note.get("aria-describedby") == "input-note-hint"


async def test_pages_declare_the_color_scheme_and_link_only_local_assets(busy: Web) -> None:
    html = (await busy.client.get("/login")).text

    assert elements(html, "meta", name="color-scheme", content="light dark")
    assert [link.get("href") for link in elements(html, "link")] == ["/static/treadle.css"]


def test_the_stylesheet_has_theme_tokens_focus_styles_and_reduced_motion() -> None:
    css = (STATIC / "treadle.css").read_text(encoding="utf-8")

    assert "color-scheme: light dark" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert "url(" not in css
