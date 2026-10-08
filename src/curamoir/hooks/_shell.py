"""Shell tokenising shared by the allowlist and the destructive-rules passes.

Separated because both passes need it: a name imported by a sibling module is
not private, and reaching past another module's underscore to get it was the
smell that prompted this split.
"""

import re
import shlex

#: Shell separators a command may hide behind. A single `&` backgrounds the left
#: side and starts a new command, so `echo hi & rm -rf /tmp/x` is two commands.
SEGMENT = re.compile(r"&&|\|\||\||;|&|\n")

#: Programs whose argument is a script or inline code rather than data.
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})


def segments(command):
    """A command split on shell separators."""
    return SEGMENT.split(command or "")


def tokenise(segment):
    """Tokens for one segment.

    `shlex` raises on unbalanced quotes; falling back to a naive split rather
    than to no inspection matters, or an unclosed quote would be a way round
    the guard.
    """
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def program_name(token):
    """The bare program name a token refers to, ignoring path and quoting."""
    return token.strip("\"'").rsplit("/", 1)[-1]
