"""Why a command was refused.

Structured so tests can assert the REASON rather than a prose substring. The
provenance review found that asserting `deny` plus a truthy message could not
distinguish these, so deleting the shell-variable rule left `$R -rf /` refused
by the allowlist instead and nothing noticed.
"""

from enum import Enum


class Category(str, Enum):
    #: Construction whose effect cannot be determined by reading it — command
    #: substitution, a shell `-c`, a variable reference, an interpreter.
    UNVERIFIABLE = "unverifiable"
    #: A recognisable command that cúramóir's jobs have no business running.
    NOT_ALLOWLISTED = "not_allowlisted"
    #: Could not be tokenised at all (unbalanced quoting).
    UNPARSEABLE = "unparseable"
    #: Allowlisted program, destructive use — caught by the denylist second pass.
    DESTRUCTIVE = "destructive"

    def __str__(self):
        return self.value
