# Contributing to Treadle

This guide explains how to set up Treadle for development, run the tests and checks, follow
the code style and the layer rules, and write commits. To report a security problem, don't
open an issue; follow [SECURITY.md](SECURITY.md) instead.

## Set up

You need macOS or Linux, Git, and [uv](https://docs.astral.sh/uv/). uv installs a suitable
Python for you; the project's `.python-version` file pins 3.12 for development, and Treadle
supports Python 3.11 and later. Some tests run real `bash`, `sh`, and `python3` processes,
which macOS and most Linux distributions include.

```sh
git clone https://github.com/kingmuja12/treadle.git
cd treadle
uv sync
uv run treadle version
```

`uv sync` creates `.venv/`, installs Treadle in editable mode, and installs the development
tools from the `dev` dependency group: pytest, pytest-cov, anyio, mypy, ruff, and the PyYAML
type stubs. It uses the versions in `uv.lock`.

## Run the tests

```sh
uv run pytest
```

To run part of the suite, pass a path or a `-k` expression:

```sh
uv run pytest tests/unit
uv run pytest tests/unit/expr -k contains
```

To measure coverage, as CI does:

```sh
uv run pytest --cov --cov-report=term --cov-report=json
uv run python scripts/check_coverage.py coverage.json
```

Branch coverage must stay at 90% or more overall. These files need 95% each: `expr/*`,
`cron.py`, `store/*`, `engine/*`, `triggers/*`, `workflow/loader.py`, and `web/security.py`.

### How the tests are organized

| Path | Contents | Rules |
|---|---|---|
| `tests/unit/` | Deterministic tests of single modules. | Async tests run on a virtual-time event loop, so sleeps and timeouts take no real time. Unit tests never start threads or subprocesses, and never open sockets. |
| `tests/integration/` | Tests with real time, real threads, real subprocesses, and real sockets, including the daemon. | Keep timeouts between 0.2 and 2 seconds, and assert the order of events rather than exact durations. |
| `tests/e2e/` | Tests that drive the installed `treadle` console script. | |
| `tests/test_architecture.py` | The [layer rules](#layer-rules). | |
| `tests/test_docs.py` | The examples, the workflows in the docs, and the README's sections. | |
| `tests/support/` | Shared fixtures and test doubles, such as `FakeStep`, `FakeClock`, the virtual-time loop, and model builders. | |

- Write async tests as plain `async def test_...` functions. The anyio pytest plugin runs
  them.
- Pass `random.Random(1234)`, or the `rng` fixture, wherever code takes a random source.
- A `DeprecationWarning` raised from Treadle's code fails the test run.
- The diagnostics tests compare each file in `tests/unit/workflow/cases/` with its expected
  `.txt` output. After you change a message on purpose, rewrite the expected files with
  `TREADLE_UPDATE_GOLDEN=1 uv run pytest tests/unit/workflow/test_golden.py`, and review the
  diff.

## Run the checks

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
python3 scripts/check_hygiene.py
uv run treadle validate --strict examples/*.yml
```

- `ruff format` fixes formatting, and `ruff check --fix` fixes many lint problems.
- mypy runs in strict mode on `src/treadle`.
- `scripts/check_hygiene.py` fails when the repository holds a secret file, such as `.env`,
  or text that looks like a private key or an access token.

CI runs the tests and these checks on Ubuntu and macOS with Python 3.11 through 3.15, and
runs mypy for each of those versions. It also builds the wheel, installs it in a fresh
environment, and runs the installed command.

## Code style

- Ruff formats the code, with lines of at most 100 characters. `pyproject.toml` lists the
  lint rules.
- Every module starts with `from __future__ import annotations`.
- Use absolute imports, such as `from treadle.units import parse_duration`. Relative imports
  are banned.
- Each package's `__init__.py` holds only a docstring. The top-level `treadle/__init__.py`
  also defines `__version__`. Import names from the module that defines them.
- Make dataclasses `@dataclass(frozen=True, slots=True)` unless they must change.
- Use timezone-aware UTC `datetime` values for times, and `float` seconds for durations. In
  SQLite, store times as integer milliseconds since the epoch, in UTC.
- Keep SQLite calls off the event loop: go through `AsyncStore`.
- Never pass user data to `eval`, `exec`, or a shell command line.
- Give each public function a docstring that says what it returns and what it raises.
  Comments explain why, not what.
- Messages start with a lowercase letter, don't end with a period, and say how to fix the
  problem when they can, as in `input 'name' has no value (write name=VALUE)`.
- Write messages and documentation in the
  [Google developer documentation style](https://developers.google.com/style): second
  person, active voice, present tense, and sentence-case headings.

## Layer rules

Treadle's modules form ten layers, from `types` and `errors` at the bottom to the CLI at the
top. [Source layout](docs/architecture.md#source-layout) lists them.

- A module may import from any lower layer, and from modules listed before it in its own
  layer. Imports under `if TYPE_CHECKING:` and inside functions count too.
- Only some modules may import third-party packages: `yaml` only in `yamlio`; `httpx` in
  `steps.base`, `steps.http`, `engine.executor`, `runner`, and `daemon`; `starlette` and
  `jinja2` in `web.*`; and `uvicorn` only in `daemon`. Everything else uses the standard
  library.
- `tests/test_architecture.py` enforces these rules, and also fails when a module exists that
  its layer list doesn't name. When you add a module, add it to `LAYERS` in that file, in the
  layer where it belongs.

When an import breaks the rules, move the shared code down a layer, or pass the dependency
in through a protocol from `treadle.protocols`.

## Documentation

- The documentation lives in `docs/`, and the examples in `examples/`.
- When you change behavior that users can see, update the docs in the same change, and add
  a line to `CHANGELOG.md`.
- Every fenced `yaml` block in `docs/` whose first line is `# treadle-example` must be a
  complete, valid workflow: `tests/test_docs.py` validates it with `--strict` rules. Leave
  the marker off fragments.
- Show real output. Run the command and copy what it prints.

## Commit conventions

- Make each commit one logical change, with its tests and documentation, and make sure the
  checks pass at every commit.
- Write the subject line in the imperative mood and in sentence case, in at most 72
  characters, without a final period: `Add jitter to retry delays`.
- In the body, explain what changed and why, wrapped at 72 characters. Refer to issues as
  `Fixes #12`.
- **A commit made with an AI coding assistant carries a `Co-Authored-By` trailer that names
  the assistant**, at the end of the message:

  ```text
  Add jitter to retry delays

  Retries of many workflows that fail at the same moment no longer
  start at the same moment.

  Co-Authored-By: Claude <noreply@anthropic.com>
  ```

## Pull requests

1. Create a branch from `main`.
2. Make your change, with tests and documentation.
3. Run the tests and the checks.
4. Open a pull request that says what the change does, why, and how you tested it.

CI must pass before a pull request is merged.
