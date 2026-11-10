# Security policy

## Supported versions

Treadle is in its first release cycle, and no version is released yet. Security fixes go to
the latest 0.x release, and until the first release, to the `main` branch.

| Version | Security fixes |
|---|---|
| The latest 0.x release | Yes |
| Older releases | No |

## Report a vulnerability

**Don't report a vulnerability in a public issue, pull request, or discussion.** Report it
privately through GitHub instead:

1. Open the repository's [Security tab](https://github.com/kingmuja12/treadle/security).
2. Click **Report a vulnerability**.
3. Describe the problem, and send the report.

Only the maintainers can read the report. Include what you can of:

- The Treadle version (`treadle version`), the operating system, and the Python version.
- What an attacker can do, and what they need first, such as network access to the daemon,
  a webhook secret, or an account on the same machine.
- Steps or a workflow file that reproduce the problem.
- Whether the problem is already public.

## What happens next

- The maintainers acknowledge the report within 7 days.
- Within 30 days, they fix the problem or share a plan to mitigate it.
- They agree a disclosure date with you, publish an advisory with the fix, and credit you
  unless you prefer otherwise.

## Scope

In scope are problems in Treadle's own code and its defaults, such as:

- Bypassing webhook signatures or replay protection.
- Reaching the API or the dashboard without the token when one is set, or getting past the
  CSRF or `Host` checks.
- A secret that shows up unmasked in a log, the database, the event file, or the daemon's
  log, in a case that the masking rules say they cover.
- A `file` step that changes a path outside the file roots, or the state directory.
- Running code through expressions or workflow files without a step that runs it.

Out of scope, by design:

- Anything that a person who controls your user account, or root, can do. Workflows run
  commands as you, without a sandbox.
- Commands that a workflow runs on purpose, including injection through a template that
  `treadle validate` warns about.
- Plain HTTP on a network. Put TLS in front of Treadle, as
  [the operations guide](docs/operations.md#tls-and-remote-access) describes.
- On loopback without a token, other local users reaching the API. Set a token on a shared
  machine.

## Masking limits

Treadle masks secret values in logs and stored data: the raw value, each line of a multi-line
value, and its JSON-escaped and URL-encoded forms. Masking can't protect a secret in these
cases:

- The value is shorter than 4 characters, or only whitespace.
- A step transforms the value, for example with base64 or a hash, before it prints it.
- A program prints the value in pieces, across separate lines.
- A step sends the value somewhere else, such as to a server, or writes it to a file.

Later steps receive outputs unmasked, because they need the exact values. For the details,
see [Masking](docs/expressions.md#masking) and the [security model](docs/security-model.md).
