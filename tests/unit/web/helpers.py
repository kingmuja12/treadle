"""Helpers for the web tests: the application under test, and HTML inspection."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

import httpx
from tests.support.builders import make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import (
    FakeRunControl,
    FakeTriggerInfo,
    FakeWebhookHandler,
    FakeWorkflowSource,
)

from treadle.config import Settings
from treadle.models import Step, Workflow
from treadle.protocols import RunReader
from treadle.web.app import WebContext
from treadle.web.security import CSRF_COOKIE

TOKEN = "correct-horse-battery-staple-0123456789"
SECRET_KEY = bytes(range(32))
BASE_URL = "http://127.0.0.1:8765"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@dataclasses.dataclass
class Web:
    """One application under test: its context, its test doubles, and a client."""

    ctx: WebContext
    client: httpx.AsyncClient
    workflows: FakeWorkflowSource
    runs: FakeRunControl
    reader: RunReader
    hooks: FakeWebhookHandler
    triggers: FakeTriggerInfo
    clock: FakeClock
    draining: bool = False

    async def csrf(self, path: str = "/") -> str:
        """Load a page and return its CSRF token, which the client keeps as a cookie.

        Forms repeat the cookie's value in their `csrf_token` field.
        """
        await self.client.get(path)
        return self.client.cookies[CSRF_COOKIE]

    def set_cookie(self, name: str, value: str) -> None:
        """Replace a cookie as if the server had set it for this host."""
        self.client.cookies.set(name, value, domain=self.client.base_url.host, path="/")

    async def post_form(
        self, path: str, data: dict[str, str] | Sequence[tuple[str, str]] = (), *, page: str = "/"
    ) -> httpx.Response:
        """Submit a form with a valid CSRF token taken from `page`."""
        token = await self.csrf(page)
        fields = list(data.items()) if isinstance(data, dict) else list(data)
        return await self.client.post(
            path, content=form_body(("csrf_token", token), *fields), headers=FORM
        )

    async def login(self, token: str = TOKEN, *, next_url: str = "/") -> httpx.Response:
        """Sign in through the login form."""
        data = {"token": token, "next": next_url}
        return await self.post_form("/login", data, page=f"/login?next={next_url}")


MakeWeb = Callable[..., Web]


@dataclasses.dataclass(frozen=True)
class Element:
    """One start tag of a page."""

    tag: str
    attrs: dict[str, str | None]

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(Element(tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def elements(html: str, tag: str | None = None, **attrs: str) -> list[Element]:
    """Return the start tags of a page, optionally only those with `tag` and `attrs`.

    Write a trailing underscore for attribute names that are Python keywords: `class_`.
    """
    collector = _Collector()
    collector.feed(html)
    wanted = {name.rstrip("_").replace("_", "-"): value for name, value in attrs.items()}
    return [
        element
        for element in collector.elements
        if (tag is None or element.tag == tag)
        and all(element.attrs.get(name) == value for name, value in wanted.items())
    ]


def unescape(html: str) -> str:
    """Return the page source with character references decoded, for substring checks."""
    return html_unescape(html)


def text(html: str) -> str:
    """Return the text of a page with whitespace collapsed, as a reader sees it."""
    collector = _Collector()
    collector.feed(html)
    return " ".join(" ".join(collector.text).split())


def form_body(*fields: tuple[str, str]) -> str:
    """Encode form fields in order, so repeated names keep their submission order."""
    return urlencode(fields)


def csrf_field(html: str) -> str:
    """Return the CSRF token that a page's forms carry."""
    match = _CSRF_FIELD_RE.search(html)
    assert match is not None, "the page has no CSRF field"
    return match[1]


def project_workflow(settings: Settings, *steps: Step, **kwargs: Any) -> Workflow:
    """Build a workflow whose file is in the project's workflows directory."""
    workflow = make_workflow(*steps, **kwargs)
    return dataclasses.replace(workflow, path=settings.workflows_dir / f"{workflow.id}.yml")
