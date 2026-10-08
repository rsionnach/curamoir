"""E0-R7: redaction for everything cúramóir writes.

Briefs, handoffs, issues and PR bodies pass through `redact` before being
written. Making this the only write path for those artefacts is deliberate: a
later epic cannot forget to call something it has to go through.

LIMITS, stated plainly and repeated in docs/security.md (E13-R2b):

- Pattern-based redaction catches shapes it knows. An unrecognised secret
  format passes through untouched.
- The generic name=value rule requires a value of at least 8 characters, so a
  short secret (`hunter2`) passes through. Shorter values are mostly ordinary
  config, and redacting them would make an artefact unreadable.
- It reduces exposure; it does not guarantee absence. A brief is committed and
  may become a public issue (E9-R3), so the real protection is not putting
  secrets in a repo cúramóir can read.

Each pattern preserves the surrounding context — the key's NAME survives, only
its value is replaced — because a brief reading "[REDACTED]" with no indication
of what was removed is not actionable.
"""

import os
import re
from pathlib import Path

PLACEHOLDER = "[REDACTED]"

# Ordered: specific vendor shapes first, generic assignments last, so a known
# key is labelled precisely rather than caught by the catch-all.
_PATTERNS = (
    # Anthropic
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), PLACEHOLDER),
    # OpenAI-style
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), PLACEHOLDER),
    # GitHub (classic, fine-grained, app, refresh, OAuth)
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), PLACEHOLDER),
    # AWS access key id, and Google API keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), PLACEHOLDER),
    # Slack
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), PLACEHOLDER),
    # PEM private keys: the whole block, not just the header. The END marker is
    # optional because a key most realistically reaches an artefact TRUNCATED —
    # as a diff hunk or a file excerpt showing the first lines of a key file —
    # and requiring the terminator let exactly that case through whole.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)",
            re.DOTALL,
        ),
        PLACEHOLDER,
    ),
    # Bearer tokens and JWTs
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 " + PLACEHOLDER),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"), PLACEHOLDER),
    # Credentials embedded in a URL
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s/@]+@"), r"\1:" + PLACEHOLDER + "@"),
    # A secret-looking QUERY PARAMETER, handled before the generic rule and with
    # its own value class. The generic rule must not have to reason about query
    # syntax: narrowing ITS value class to stop a query string being swallowed
    # truncated every secret containing & or ;, and left one whose leading part
    # fell under the 8-character floor unredacted entirely.
    (
        re.compile(
            r"(?i)([?&][A-Za-z0-9_.\-]*"
            r"(?:password|passwd|secret|token|api[_\-]?key|access[_\-]?key|sig|signature|credential)"
            r"[A-Za-z0-9_.\-]*=)"
            r"(?!\[REDACTED\])[^\s&;\"']+"
        ),
        r"\1" + PLACEHOLDER,
    ),
    # Generic: a secret-looking NAME assigned a long-enough value. The name is
    # kept so the reader knows what was removed.
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_.\-]*(?:password|passwd|secret|token|api[_\-]?key|"
            r"access[_\-]?key|private[_\-]?key|credential)[A-Za-z0-9_.\-]*)"
            r"(\s*[:=]\s*)"
            # The original class, restored. A narrower one truncated secrets
            # containing & or ;. The negative lookahead keeps this rule from
            # re-matching a value the query-parameter rule already replaced,
            # which is what would otherwise eat the rest of the query string.
            r"[\"']?(?!\[REDACTED\])([^\s\"',]{8,})[\"']?"
        ),
        r"\1\2" + PLACEHOLDER,
    ),
)


def redact(text):
    """Return `text` with recognised secrets replaced by a placeholder.

    Idempotent: redacting already-redacted text is a no-op, because the
    placeholder matches none of the patterns.
    """
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sweep_stale_temporaries(target):
    """Remove `<target>.<pid>.tmp` files no live process is writing.

    A process killed between opening the temp and renaming it cannot clean up
    after itself, and `.curamoir/` is committed — so without this, every killed
    hook leaves one stray in the repository forever.
    """
    for stale in target.parent.glob(f"{target.name}.*.tmp"):
        middle = stale.name[len(target.name) + 1:-len(".tmp")]
        if not middle.isdigit() or int(middle) == os.getpid():
            continue
        try:
            os.kill(int(middle), 0)
        except ProcessLookupError:
            stale.unlink(missing_ok=True)   # its writer is gone
        except OSError:
            continue                        # alive, or not ours to judge


def write_artefact(path, text, encoding="utf-8"):
    """Write one of cúramóir's generated artefacts, redacted.

    The single write path E0-R7 calls for. Briefs (E9-R3), handoffs (E8-R1),
    issue bodies and PR bodies (E6-R2, E3-R3) all go through here, so redaction
    is structural rather than something each epic has to remember.

    Written atomically: into a temporary file beside the target, then renamed
    over it. `write_text` truncates first and writes second, so a write that
    fails part way — a full disk, a quota — left a FRAGMENT where a complete
    artefact had been, with the previous one already gone. SessionStart would
    then inject the fragment next session as state from the previous session.
    `os.replace` is atomic within a directory, so a reader sees the old complete
    artefact or the new one and never a partial one.
    """
    # Redacted BEFORE the file is opened. The regex pass is the slow part — a
    # 10 MB artefact takes 5.6s — and running it inside the `with` held the
    # temp file open for all of it, so a hook killed at its timeout left a
    # stray behind. `except OSError` cannot clean up after SIGKILL.
    redacted = redact(text)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temporaries(target)
    # Beside the target, not in /tmp: `os.replace` is only atomic within one
    # filesystem, and a repo on an external volume would otherwise degrade to a
    # copy — the very non-atomic write this exists to avoid.
    #
    # The name carries the pid, so two writers never share one temp file. With a
    # single `<name>.tmp` they raced: both wrote it, the first renamed it, and
    # the second's `os.replace` raised FileNotFoundError on a path that had just
    # been moved out from under it. `write_handoff` catches that and reports no
    # handoff, so two sessions ending together silently lost one.
    scratch = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with scratch.open("w", encoding=encoding) as handle:
            handle.write(redacted)
            handle.flush()
            # The rename is atomic, but without the fsync the CONTENT may still
            # be in flight: a crash can leave a present, empty, renamed file.
            os.fsync(handle.fileno())
        os.replace(scratch, target)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise
    return target
