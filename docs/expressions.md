# Expressions

Expressions let a workflow use values that exist only at run time: inputs, secrets, trigger
data, and the outputs of earlier steps. They appear inside `${{ ... }}` templates and, bare,
in a step's `if`.

Treadle evaluates expressions with its own parser and evaluator. An expression can only read
JSON values and call the functions on this page. Nothing reaches Python's `eval` or `exec`.

For the keys that accept expressions, see
[Where expressions are allowed](workflow-syntax.md#where-expressions-are-allowed).

## Templates

A template is a string that holds one or more `${{ expression }}` parts.

- A string that is exactly one `${{ expression }}`, with only whitespace around it, gives the
  expression's value with its type. When `n` is an integer input, `count: ${{ inputs.n }}`
  gives an integer, and `json: ${{ fromJSON(inputs.payload) }}` gives an object.
- A string that mixes text and expressions converts each value to its
  [display string](#display-strings) and joins the parts. The result is a string.
- `env` values are always strings, even when the value is a single expression.
- Treadle renders `with` values at any depth, through lists and mapping values. Keys are
  never templates, and strings without `${{` stay as they are.
- To write a literal `${{`, use `${{ '${{' }}`.

The end of an expression is the first `}}` outside a string literal.

## Conditions

A step's `if` takes a bare expression, without `${{ }}`:

```yaml
if: trigger.body.ref == 'refs/heads/main'
```

Treadle also accepts a single `${{ ... }}`, which it unwraps, and the YAML booleans `true`
and `false`. An `if` that mixes text and `${{ }}` parts is an error (E201).

When a condition calls no status function, Treadle evaluates it as `success() && (...)`, as
GitHub Actions does. For that reason, after an earlier step fails, a step runs only when its
condition calls a status function such as `failure()` or `always()`. See
[Status functions](#status-functions).

In YAML, write a condition as a plain scalar, without quotes around it. A condition that
starts with `!` needs double quotes, because YAML reads a leading `!` as a tag:

```yaml
if: "!inputs.dry_run"
```

## Literals

| Literal | Examples | Notes |
|---|---|---|
| Null | `null` | |
| Booleans | `true`, `false` | |
| Numbers | `3`, `-1`, `0.5`, `1e3` | JSON number syntax. |
| Strings | `'main'`, `'it''s'` | Single quotes only. Write `''` for a quote inside a string. There are no backslash escapes. |

A double quote in an expression is E201 with the hint "use single quotes". When a template
with string literals is a YAML value, put the whole value in double quotes, as in
`message: "${{ format('{0} done', run.id) }}"`.

## Operators

From highest to lowest precedence:

| Operators | Meaning |
|---|---|
| `a.b`, `a[i]` | Member and index access |
| `!a` | Not. Returns a boolean. |
| `<`, `<=`, `>`, `>=` | Comparison |
| `==`, `!=` | Equality |
| `&&` | And. Returns the first falsy operand, or the last operand. |
| `\|\|` | Or. Returns the first truthy operand, or the last operand. |

Use parentheses to group. Comparisons don't chain: `a < b < c` is E201 with the hint
"combine comparisons with &&".

Because `&&` and `||` return an operand, `inputs.name || 'world'` works as a fallback for an
empty or missing value.

There is no arithmetic. A `-` is part of a name, as in `steps.build-image.outputs.tag`, or
the sign of a number literal such as `-1`.

## Names and access

A name starts with a letter or `_` and continues with letters, digits, `_`, and `-`. After a
`.`, any name is a property name, including `true`, `false`, and `null`.

- `a.b` returns the member `b` of the object `a`. It returns `null` when the member is
  missing or `a` isn't an object.
- `a[i]` on an array takes an integer index, or a float with no fractional part. A negative
  or out-of-range index returns `null`. Any other index type is an error.
- `a['key']` on an object takes a string key. A number key uses its display string, so
  `a[1]` reads the key `'1'`.
- An index on `null` or on any other type returns `null`.

## Contexts

| Context | Contents | Available in |
|---|---|---|
| `inputs` | The run's input values, converted to their declared types. An undeclared name is E207. | Everywhere |
| `env` | In workflow `env`: the environment Treadle started with, without `TREADLE_*` and secret variables. In a step's `if` and `env`: that environment plus the workflow `env`. In `with` and `outputs`: both, plus the step `env`. A missing name is `null`. | Everywhere |
| `secrets` | `secrets.NAME` is the variable `TREADLE_SECRET_NAME` from the environment, or else `NAME` from `.env`. See [Secrets](#secrets). | Everywhere |
| `trigger` | The trigger data. A manual run has `{"kind": "manual", "origin": "cli"}`, with the origin `cli`, `api`, or `dashboard`. For the other kinds, see [Triggers](triggers.md). | Everywhere |
| `run` | `id` (integer), `attempt` (integer), `started_at` (ISO 8601 in UTC, ending in `Z`), `workflow` (the workflow ID), `trigger` (the trigger kind), and `rerun_of` (integer or `null`). | Everywhere |
| `workflow` | `id`, `name`, `description`, `path` (relative to the project directory), and `digest` (the first 12 hex characters of the file's SHA-256). | Everywhere |
| `steps` | `steps.<id>.outputs.<name>` and `steps.<id>.status`, for the steps that this step depends on, directly or through other steps. | A step's `if`, `env`, `with`, and `outputs` |
| `result` | This step's own outputs. | A step's `outputs` only |

`steps.<id>.status` holds the step's status as recorded: `succeeded`, `failed`, `timed_out`,
`cancelled`, or `skipped`. A tolerated failure (`continue_on_error: true`) still shows
`failed` or `timed_out`.

## Types and comparisons

The value types are `null`, boolean, number, string, array, and object. Numbers are never
`NaN` or infinite.

### Truthiness

`false`, `null`, `0`, `0.0`, and `''` are falsy. Every other value is truthy, including
`'0'`, `'false'`, an empty array, and an empty object.

### Equality

`==` and `!=` never fail:

- Values of the same type compare by value. **Strings compare case-sensitively.** Arrays and
  objects compare element by element.
- A number and a string compare as numbers when the string is a valid JSON number, with no
  spaces around it. Otherwise they're unequal. So `steps.count.outputs.n == 3` works with the
  string outputs of a `shell` step.
- Any other pair of different types is unequal: `'true' == true`, `null == 0`, and
  `true == 1` are all `false`. Booleans are never numbers.

### Comparison

`<`, `<=`, `>`, and `>=` accept two numbers, two strings (compared by code point), or a
number and a numeric string. Any other pair is an error, such as "cannot compare null with
number", instead of a silent `false`.

## Functions

Function names are case-sensitive. An unknown name is E204, with a suggestion such as "did
you mean 'startsWith'?".

| Function | Result |
|---|---|
| `contains(haystack, needle)` | For a string, whether it contains the needle's display string, case-sensitively. A `null` needle gives `false`. For an array, whether any element `==` the needle. A `null` haystack gives `false`. |
| `startsWith(s, prefix)`, `endsWith(s, suffix)` | Whether `s` starts or ends with the other value, case-sensitively. Scalars use their display strings. |
| `format(fmt, args...)` | `fmt` with `{0}`, `{1}`, and so on replaced by the arguments' display strings. `{{` and `}}` give literal braces. |
| `join(items, sep)` | The display strings of an array's elements, joined with `sep` (default `,`). A string comes back as it is, and `null` gives `''`. |
| `toJSON(value)` | Compact JSON on one line. |
| `fromJSON(text)` | The value that strict JSON text holds. Rejects `NaN`, `Infinity`, input over 1 MiB, and nesting deeper than 64 levels. |
| `lower(s)`, `upper(s)`, `trim(s)` | The string in lowercase or uppercase, or without surrounding whitespace. Scalars use their display strings. |
| `length(value)` | The characters in a string, the elements in an array, or the keys in an object. `null` gives 0. |
| `default(value, fallback)` | `fallback` when `value` is `null`, otherwise `value`. Use `\|\|` to replace any falsy value. |
| `success()`, `failure()`, `always()`, `cancelled()` | Status functions. They're allowed only in `if` (E205). |

A function fails when an argument has a type it can't use, such as an object passed to
`lower`. A function that builds a string fails when the result would be longer than 1 MiB.

## Status functions

Status functions look only at the steps that the current step depends on, directly or
through other steps. They never look at steps that merely finished earlier, so the result
doesn't depend on timing.

| Function | True when |
|---|---|
| `success()` | The run isn't cancelling, and no step that this step depends on failed, timed out, or was cancelled. Skipped steps don't count against it. |
| `failure()` | A step that this step depends on failed or timed out. |
| `cancelled()` | The run is cancelling, for any reason: a user cancel, a replacing run, the run timeout, or a shutdown. |
| `always()` | Always. |

A tolerated failure counts as passing for these functions.

Common patterns:

| Condition | Runs |
|---|---|
| none, or `success()` | when the steps it depends on passed or were skipped |
| `failure()` | to alert after a failure |
| `always()` | to clean up, whatever happened |
| `cancelled()` | only when the run is being cancelled |
| `steps.build.status == 'succeeded'` | only when `build` ran and succeeded, not when it was skipped |

## Display strings

When a template mixes text and expressions, or renders an `env` value, Treadle converts each
value to its display string:

| Value | Display string |
|---|---|
| `null` | `''` |
| `true`, `false` | `true`, `false` |
| An integer | Decimal digits, such as `42` |
| A float with no fractional part | No decimal point, such as `3` |
| Any other float | Python's shortest form, such as `0.1` |
| A string | The string itself |
| An array or object | Compact JSON |

## Secrets

`secrets.NAME` reads, in order:

1. The environment variable `TREADLE_SECRET_NAME`.
2. The entry `NAME` in the project's `.env` file.

Every entry in `.env` is a secret, so Treadle masks its value. An ordinary environment
variable without the `TREADLE_SECRET_` prefix isn't a secret.

When a template names a secret that isn't defined, `treadle validate` warns with W201 inside
a project, and the step fails at run time. Treadle never passes a secret to a step on its
own: write `${{ secrets.NAME }}`, usually in the step's `env`.

```yaml
# treadle-example
name: Post a notification
steps:
  - id: notify
    env:
      NTFY_URL: ${{ secrets.NTFY_URL }}
    run: curl --fail --silent --data "Run ${TREADLE_RUN_ID} finished" "$NTFY_URL"
```

While `treadle serve` runs, it reads `.env` again when the file changes, so a changed secret
applies to new runs and webhook checks without a restart. The API token is the exception:
it takes effect only after a restart. See [Signals](operations.md#signals).

### The `.env` file

`.env` holds one entry per line, in UTF-8:

```sh
# A comment line
NTFY_URL=https://ntfy.sh/my-topic
export BACKUP_HOST=nas.local     # `export` is optional, and ` #` starts a comment
PASSWORD="correct horse #battery"
GREETING="line one\nline two"
PATTERN='^v[0-9]+\.[0-9]+$'
```

- Each line is blank, a comment that starts with `#`, or `KEY=VALUE`, with an optional
  `export ` before it. A key uses the environment variable name pattern.
- An unquoted value runs to the end of the line, without the spaces around it. A space or a
  tab followed by `#` starts a comment, so `PASSWORD=correct horse #battery` sets
  `correct horse`. Quote every value that holds a space or a `#`.
- Single quotes keep the text exactly as written, on one line.
- Double quotes can span lines, and turn `\n`, `\t`, `\"`, and `\\` into a newline, a tab,
  a double quote, and a backslash. Any other backslash stays as written.
- After a closing quote, the line can hold only spaces and a comment.
- Treadle doesn't expand variables, so `$HOME` in a value stays `$HOME`.
- When a key appears twice, the last value wins.
- Treadle ignores one UTF-8 byte-order mark at the start of the file, which some editors
  write, and accepts Windows line endings.

When a line doesn't parse, the command stops with exit code 3, and the error names the file
and the line, never the value:
`error: /home/ada/automations/.env:2: the value of BAD has no closing quote, or text after it`.
While `treadle serve` runs, a change that doesn't parse keeps the previous secrets, and the
daemon logs a warning.

### Masking

Treadle replaces each secret value with `***` in every log line, in stored inputs, trigger
data, step outputs, and errors, in the event file, and in the daemon's log. It masks:

- The value itself.
- Each line of a multi-line value.
- The value in JSON-escaped form and in URL-encoded form.
- A number whose JSON text contains a secret value, such as a numeric chat ID, in stored
  inputs, trigger data, and outputs, and in the event file. The number becomes the string
  `***`.

Masking has limits:

- Values shorter than 4 characters, and values made only of whitespace, aren't masked.
  `treadle validate` warns about a short secret with W202.
- A transformed value, such as a base64-encoded secret, isn't masked.
- A secret split across two log lines isn't masked.
- Later steps receive outputs unmasked, because they need the exact values. Treadle masks
  only the stored copies.
- Treadle stores inputs masked, so a re-run of a run whose input held a secret runs with
  `***` in its place. When that input is an `integer` or a `number`, the re-run fails input
  validation instead, because `***` isn't a number.

To mask a value that a step creates at run time, print `::add-mask::VALUE` to standard
output. See [Masking values that a step creates](workflow-syntax.md#masking-values-that-a-step-creates).

## Evaluation order

1. When a run starts, Treadle renders the workflow `env` once, in the order you wrote it.
   Each value sees only the environment that Treadle started with, so workflow `env` values
   can't refer to each other.
2. For each step, Treadle evaluates `if` first.
3. Then it renders the step `env`. Each value sees the environment plus the workflow `env`.
4. Then it renders `with`, once per step, not once per attempt.
5. After the final attempt succeeds, it evaluates `outputs`.

## Limits

| Limit | Value |
|---|---|
| Length of one expression | 4,096 characters |
| Nesting of parentheses, brackets, and calls | 32 levels |
| Length of a rendered string | 1 MiB of characters |

Going past a limit is E201 when Treadle loads the file, or an evaluation error at run time.

## Errors

- Treadle reports syntax errors, unknown names, and wrong argument counts when it loads the
  file, with codes E201 through E212. For the list, see
  [Diagnostic codes](workflow-syntax.md#diagnostic-codes).
- An error during evaluation fails the step without a retry. The message names the field,
  such as `in steps.deploy.with.url: cannot compare null with number`.
- An error in the workflow `env` fails the run with the reason `setup_failed`, and every step
  is skipped.

## Examples

This workflow compares a step output with a number, reads JSON, and falls back to a default:

```yaml
# treadle-example
name: Expression tour
inputs:
  name:
    type: string
steps:
  - id: count
    run: echo "n=3" >> "$TREADLE_OUTPUT"
  - id: listing
    with:
      capture_stdout: true
    run: |
      echo '{"items": ["a", "b"]}'
  - id: summary
    uses: log
    with:
      message: "${{ format('{0} items, n is 3: {1}, hello {2}', length(fromJSON(steps.listing.outputs.stdout).items), steps.count.outputs.n == 3, inputs.name || 'world') }}"
```

Its last step logs `[info] 2 items, n is 3: true, hello world`.
