"""The one place this project decides what a secret looks like.

WHY THIS IS A TOP-LEVEL MODULE WITH NO IMPORTS
----------------------------------------------
`redact()` used to live in `broker/remote.py`, which is the module the broker
logs through. That was the right place for the broker and the wrong place for
everything else, and the gap was not theoretical:

  * `fleetctl` printed a raw `{exc}` from `api_credit()` and `api_instances()`
    at five sites. Those call the vast.ai SDK, whose `HTTPError` message is the
    request URL, and the SDK puts the API key in the QUERY STRING. A human runs
    `fleetctl` interactively and pastes its output into a chat window.
  * `vastctl/vastctl.py` — the module that talks to vast.ai and therefore the
    one closest to the key — had no redaction of any kind, including in its
    top-level `except VastError` handler.
  * `broker/diagnostics.py` formats full tracebacks into the log from four
    hooks. `traceback.format_exception` ends with `str(exc)`, so an *unhandled*
    SDK error wrote the key to `broker.log` even though `diagnose()` next door
    was carefully redacting the handled ones.

So the fix was applied at one call site and reported as done, which is the
failure mode `docs/BROKEN-INSTRUMENTS.md` in the sibling repo is a catalogue of.
This module has no project imports and lives at the repository root so that the
broker, the worker-side tools, the CLIs and the diagnostics hooks can all reach
it without an import cycle and without one of them quietly keeping its own copy.

WHAT IS REDACTED, AND THE ONE THING DELIBERATELY NOT
----------------------------------------------------
Redacted:
  api_key=<value>                  the shape the SDK actually produces today
  <any param>=<64 hex> inside a console.vast.ai URL
                                   the shape it produces if the parameter is
                                   ever renamed
  Authorization: Bearer <value>    the shape `docs/operations.md` tells a human
                                   to type by hand with curl
  X-Api-Key: <value>               the other conventional header
  "api_key": "<value>"             JSON/dict serialisation, e.g. in a manifest

NOT redacted, on purpose: a bare 64-hex token with no key-ish context around it.
That is also exactly the shape of a sha256, and this project's frame integrity
checks, `frames.sha256`, and `seq.write_manifest` are built on full sha256
digests. Blanket-redacting 64 hex characters would not make the tool safer, it
would corrupt the manifests and silently break every "does this frame match"
diagnostic — turning a security control into a data-integrity bug. The context
requirement is the whole design, not an oversight.
"""
from __future__ import annotations

import re

REDACTED = "<redacted>"

# Each pattern keeps group 1 (the label) and replaces the value after it, so a
# redacted line still says what KIND of thing was removed. A line that reads
# `api_key=<redacted>` is evidence; a line with the parameter deleted entirely
# is indistinguishable from a request that never carried a key.
_PATTERNS: tuple[re.Pattern, ...] = (
    # api_key=... in a query string. Value ends at &, whitespace, quote or #.
    re.compile(r"(api[_-]?key=)[^&\s\"'#]+", re.I),
    # A renamed parameter carrying something key-shaped, but ONLY inside a
    # vast.ai URL, so a sha256 in ordinary prose is untouched.
    re.compile(r"(console\.vast\.ai[^\s\"']*?[?&][A-Za-z0-9_\-]+=)"
               r"([0-9a-fA-F]{32,})"),
    # Authorization: Bearer <token>
    re.compile(r"(authorization\s*:\s*bearer\s+)[^\s\"'&]+", re.I),
    # X-Api-Key: <token> / X_API_KEY: <token>
    re.compile(r"(x[_-]api[_-]key\s*:\s*)[^\s\"'&,}]+", re.I),
    # "api_key": "<value>" in JSON or a dict repr, both quote styles.
    re.compile(r"([\"']api[_-]?key[\"']\s*:\s*[\"'])[^\"']+", re.I),
)


def redact(text: str) -> str:
    """Return `text` with every credential-shaped value replaced.

    Total: never raises, and accepts a non-str so that a logging call site can
    hand it whatever it has without growing a try/except. A redactor that can
    throw is a redactor that gets wrapped in a bare `except` and bypassed.
    """
    if not isinstance(text, str):
        text = str(text)
    for rx in _PATTERNS:
        text = rx.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def redact_exc(exc: BaseException) -> str:
    """A never-empty, secret-free one-line description of any exception.

    `str(exc)` is empty for more exception types than is comfortable — a bare
    `RuntimeError()`, a `KeyError()` with no args — and printing one of those
    is how a failure reaches a human with its cause erased. Prefixing the class
    name makes an empty message impossible by construction.
    """
    text = str(exc).strip()
    return redact(f"{type(exc).__name__}: {text}" if text
                  else f"{type(exc).__name__} (no message)")


def redact_tb(exc_type, exc, tb) -> str:
    """A formatted traceback, redacted.

    The traceback's LAST line is `str(exc)`, which is where the SDK puts the
    URL, which is where the SDK puts the key. Every hook in
    `broker/diagnostics.py` must go through this rather than through
    `traceback.format_exception` directly.
    """
    import traceback  # stdlib, imported lazily to keep this module import-free
    return redact("".join(traceback.format_exception(exc_type, exc, tb)))
