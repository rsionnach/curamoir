"""Differential test: ask bash what a command does, then ask the guard.

Every quoting bug in this module was found by someone thinking of a spelling —
`$(...)`, then `<<EOF` inside quotes, then `&` inside quotes, then two
apostrophes inside double quotes, then `\\'`. Five rounds of that, each one a
spelling short, which is the signature of a test suite that can only contain
cases its authors imagined.

So this one does not imagine. For each construct below it puts
`$(touch EXPANDED)` inside, runs it under real bash in an empty directory, and
asks the FILESYSTEM whether bash expanded it. Then it asks `judge_command`. The
invariant is the one the docstrings claim and nothing checked:

    bash expands it  ==>  the guard denies it

`touch` is allowlisted and is not a dangerous program, so the destructive pass
cannot be what denies these: the only thing that can is pass 1's substitution
detection, which isolates the quoting layer exactly.

The reverse direction is asserted separately and loosely, because a guard that
refuses something harmless is a cost rather than a hole — and two such
divergences are known and accepted below.
"""

import shutil
import subprocess

import pytest

from curamoir.hooks import judge_command

#: `{payload}` is substituted with a command substitution that leaves a mark.
CONSTRUCTS = [
    "echo {payload}",
    "echo '{payload}'",
    'echo "{payload}"',
    "echo \\'{payload}\\'",
    "echo \\\\'{payload}'",
    "echo \"don't {payload} won't\"",
    "echo \"a' {payload} 'b\"",
    'echo "a\\" {payload}',
    "echo ''{payload}''",
    "echo \\'\\'{payload}",
    "echo 'a'\"'\"'b {payload}'",
    "echo $'{payload}'",
    'echo "\\{payload}"',
    "echo 'it is '{payload}' now'",
    "echo \"'\" {payload} \"'\"",
    "echo 'a\nb' {payload}",
    "echo 'a\\\\b' {payload}",
    "echo 'a`b' {payload}",
    "echo {payload} \\",
    "echo \\\n{payload}",
    "printf '%s' {payload}",
    "echo \"a'b'c' {payload}\"",
]

PAYLOAD = "$(touch EXPANDED)"

#: Divergences that are known, accepted and in the SAFE direction: bash does
#: not expand these, and the guard refuses them anyway. Each costs a retry and
#: has a working alternative, which is the trade DEC-9 was taken to make.
#: Listed rather than silently tolerated so that a new one is a test failure.
ACCEPTED_STRICT = {
    # The scan keeps double-quoted content verbatim, so an escaped `$` still
    # matches. `git commit -m "fix \$HOME docs"` is refused; the single-quoted
    # form works. Fixing it means a fourth interacting rule in a function that
    # currently has no bypasses.
    'echo "\\{payload}"',
    # An escaped double quote leaves the string unterminated, so `lex` returns
    # None and the command is refused as UNPARSEABLE. Bash does not expand it
    # either — it is waiting for the quote to close — so refusing a command
    # that cannot be parsed is the correct answer rather than a divergence to
    # fix. Found by this test on its first run, which is the point of it: the
    # construct was not in the 34 forms a reviewer had thought of.
    'echo "a\\" {payload}',
    # ANSI-C quoting is refused whatever it holds (R5 edge-case review):
    # `$'...'` decodes escapes, so `git reset -$'-hard'` is `--hard` in a
    # spelling no word-level rule sees. Bash expands nothing inside it; the
    # refusal is for the decoding, and a plain single-quoted string works.
    "echo $'{payload}'",
}


def bash_expands(command, tmp_path):
    """Whether real bash performs the substitution in `command`."""
    marker = tmp_path / "EXPANDED"
    if marker.exists():
        marker.unlink()
    subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    return marker.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to compare against")
@pytest.mark.parametrize("construct", CONSTRUCTS)
def test_the_guard_denies_whatever_bash_would_expand(construct, tmp_path):
    command = construct.format(payload=PAYLOAD)

    expanded = bash_expands(command, tmp_path)
    denied = judge_command(command, True).deny

    if expanded:
        assert denied, (
            f"bash expanded {command!r} and the guard allowed it — pass 1's "
            "substitution detection did not see it"
        )
    elif denied:
        assert construct in ACCEPTED_STRICT, (
            f"a NEW false refusal: bash does not expand {command!r}. Either fix "
            "it or add the construct to ACCEPTED_STRICT with the reason."
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to compare against")
def test_the_accepted_strict_list_does_not_rot():
    """Every entry must still be a construct we test, and still be refused.

    An accepted divergence that has silently become correct should leave the
    list, or the list stops describing the code.
    """
    for construct in ACCEPTED_STRICT:
        assert construct in CONSTRUCTS, f"{construct!r} is no longer tested"
        assert judge_command(construct.format(payload=PAYLOAD), True).deny, (
            f"{construct!r} is now allowed; remove it from ACCEPTED_STRICT"
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to compare against")
def test_the_oracle_can_actually_detect_an_expansion(tmp_path):
    """A differential test whose oracle always says "no" passes vacuously."""
    assert bash_expands(f"echo {PAYLOAD}", tmp_path), "the marker never appeared"
    assert not bash_expands(f"echo '{PAYLOAD}'", tmp_path), "single quotes expanded?"
