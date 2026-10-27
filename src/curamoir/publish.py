"""E3-R3's last step: the sweep's accepted change leaves the machine as one
commit on one branch and one PR (cm-fkd.12; fix_scope's runner step 9).

**What the runner calls**: `origin_gh_repo(repo)` once per sweep (its value
also goes to github.open_sweep_prs), then `publish(...)`, which returns a
Published or raises one of the errors in "Errors" below. The other names in
__all__ are publish's building blocks (check_target, pr_base, user_identity,
commit_from_patch, push_branch, delete_remote_branch, create_pr,
find_open_pr, Identity, CreateOutputUnreadable) and test seams (the
timeouts and LOOKUP_ATTEMPTS, LOOKUP_DELAY).

`publish(repo, ...)` takes what the runner holds after the gate (cm-fkd.14
calls it): the patch of exactly `gate.paths_for_pr` (GateResult.patch),
those paths, the base commit, and sweep_pr's branch, commit message, title
and body. In order, and each step only if the one before succeeded:

1. **The target branch** is judged by guards.default_branch's
   DefaultBranchGuard with `target_branch` (E0-R1, at push time: job_guard's
   docstring, fix_scope step 0 (i)): the default branch, a branch outside
   `guardrails.branch_prefix`, or one outside its `sweep-` prefix
   (idempotence.sweep_branch_prefix, which the cap and the duplicate check
   match) is refused. So is a name git would not take as a branch
   (`git check-ref-format`), one starting with `-`, or one holding a NUL
   (no argv can carry it).
2. **Which repository** (below): `gh_repo` must still be origin's.
   **The PR's base** is then GitHub's answer, never a local guess
   (pr_base): `gh repo view <gh_repo> --json defaultBranchRef`. WHY:
   detect_default_branch falls back to the local branches when
   `refs/remotes/origin/HEAD` is missing (origin's default `develop`
   would get a PR into `main`), and a stale origin/HEAD names a default
   renamed since. So the checkout must agree: its origin/HEAD, when set,
   must name that branch (else `git remote set-head origin -a`), and
   `refs/remotes/origin/<default>` must exist (else `git fetch origin`).
   The sweep branch may not be that branch either. A gh failure here is a
   refusal: nothing has been pushed. The title and body are checked here
   too, before anything is pushed: both must encode as UTF-8 and the
   title may hold no NUL (it is an argument), so a PR that gh could not
   be given never costs a push, a hook run and a deletion.
3. **The author** is the user's git identity as the checkout's git config
   gives it (user decision, 2026-10-23), with git's own precedence for
   config: `author.name`/`author.email`, else `user.name`/`user.email`,
   for the author; `committer.*`, else `user.*`, for the committer. NOT
   honoured: the GIT_AUTHOR_*/GIT_COMMITTER_* variables (every GIT_* is
   dropped) and EMAIL, which git falls back to only when no email is
   configured: an unset `user.email` (or name) is refused instead, so a
   commit is never signed with a guessed `user@hostname`.
4. **The commit** is built in the checkout's object store and nowhere else
   (commit_from_patch): a temporary index (GIT_INDEX_FILE) read from the
   base, the patch applied to it (`git apply --cached`), `write-tree`,
   `commit-tree -p <base>`. WHY: the user's work tree, index and HEAD are
   never touched, so their uncommitted work stays theirs, and nothing but
   the patch reaches the tree: not `.curamoir/state/` (the runner's
   ledger and snapshot, written in the checkout while the sweep ran; cm-sto),
   not a file a hook wrote, not a file the user staged. Never `git add -A`.
   Then `git diff-tree base <sha>` must name exactly `paths`, or nothing
   is pushed (cm-fkd.3's obligation (1): commit exactly the accepted
   paths). Hardened git (git_guard.git) throughout: no hook, fsmonitor or
   global config runs. No signing program runs either: `git commit-tree`
   does not read `commit.gpgSign` (git 2.39: only `-S` signs; observed
   with a signer that leaves a marker, which `git commit` ran and
   `commit-tree` did not), and `--no-gpg-sign` says so explicitly.
5. **The branch must be new on origin**: `git ls-remote` of origin's
   push URL (`git remote get-url --push origin`, the repository the push
   goes to, not the fetch URL of a triangular setup). A branch already
   there is someone's, perhaps an earlier sweep's with its PR open, and is
   never moved or, below, deleted. This is the early, readable refusal;
   the lease below is what holds.
6. **The push**: `git push --porcelain --force-with-lease=refs/heads/
   <branch>: origin <sha>:refs/heads/<branch>` from the checkout, with
   the user's git and config (hardened git has no credentials; user
   decision), so pre-push hooks RUN: a hook that fails is a PushFailed
   and no PR. The lease's expected value is empty: the ref must NOT exist
   on origin, which origin checks as it applies the push, and origin must
   report the ref as new (`*`). WHY: between the ls-remote and the push
   someone may create the branch at an ancestor of the sweep's commit,
   and a plain push would fast-forward it; with the lease it is rejected
   (`[rejected] (stale info)`, a PushFailed) and left where they put it;
   if it appears while the push is under way, origin itself refuses
   (`reference already exists`), with the same message.
   The lease can only refuse: it never overwrites, since the one state it
   accepts is "absent". One case passes the lease: the branch appeared at
   the sweep's own commit (another publish of the same base in the same
   second builds the same SHA), which git reports `=` [up to date] with
   exit 0, so publish requires `*` and treats `=` as "appeared", the same
   PushFailed. Never `--force`, never `+`; no tags
   (`--no-follow-tags`), no submodules. Bounded by PUSH_TIMEOUT (a hook
   may run a test suite); at it the whole process group is killed, and
   PushFailed says whether the branch arrived is unknown.
7. **The PR**: `gh pr create --repo <gh_repo> --head <branch> --base
   <default> --title <title> --body-file <file>`, through github.run_gh.
   The body goes in a temporary file (mode 0600, removed after): run_gh
   gives gh no stdin, so `--body-file -` would open a PR with an EMPTY
   body and no sweep marker, and the body is free text that does not
   belong in the argv a GhError shows. gh prints the PR's URL (`gh pr
   create --help`: "Upon success, the URL of the created pull request
   will be printed"); its last line must be a PR URL on `gh_repo`, whose
   number is the PR's.

**When the PR does not open.** What `gh pr create` did decides what
publish does next (_open_pr):

| create                   | lookups            | then                             |
|--------------------------|--------------------|----------------------------------|
| ok                       | none               | Published                        |
| GhTimeout or             | up to              | found: Published;                |
| CreateOutputUnreadable   | LOOKUP_ATTEMPTS,   | none: delete, PrCreateFailed     |
| (the outcome is unknown) | LOOKUP_DELAY apart | (`timed_out` if a timeout);      |
|                          |                    | lookup error: PrOutcomeUnknown,  |
|                          |                    | branch left                      |
| any other GhError        | one                | the same three outcomes          |
| OSError or ValueError    | none               | delete, PrCreateFailed           |

WHY each row. A timeout or unreadable output means gh may have opened the
PR; GitHub may still be finishing one whose gh was stopped, hence several
lookups. A non-zero exit can come after GitHub made the PR (a 5xx), hence
one. An OSError or ValueError is not gh's (the body file cannot be made or
written, a lone surrogate in the body, a NUL in an argument): gh never ran
with it, so there is certainly no PR, and it is still a PrCreateFailed,
never an exception from outside the family that would leave the branch
behind. The lookup is `gh pr list --head <branch>`; the PR is the one
same-repository PR from the branch whose head is the sweep's commit
(`headRefOid`) and whose base is the default branch (`baseRefName`). A
lookup that fails leaves the branch on purpose: deleting the head branch
of a PR that does exist would close it, and the next sweep's listing finds
that PR (its body carries the marker) and holds its files.

"Delete" is `git push --force-with-lease=refs/heads/<branch>:<sha> origin
--delete`, bounded by REMOTE_TIMEOUT (hooks run here too), leased to the
sweep's own commit: a branch someone has pushed to since is NOT deleted
(`stale info`) and their commit stays. PrCreateFailed carries both
outcomes: why the PR failed, and whether the deletion worked, with its
error when it did not (reported, never masked: the branch may then still
be on origin, to check and delete by hand).

**Which repository.** gh picks the repository it talks to from `gh repo
set-default`, else by remote priority (upstream, then github, then
origin), while the push goes to origin. In a fork's clone those differ:
the cap and the duplicate check would read upstream's PRs while the PR
opened on origin, or the PR would open on upstream from a branch it does
not have. So origin_gh_repo names origin's repository once, as gh's
`--repo` takes it (`HOST/OWNER/REPO`, always with the host, so GH_HOST
cannot move it), from origin's push URL as configured (`pushurl`, else
`url`); the caller passes that value to the open-PR listing
(github.open_sweep_prs's `gh_repo`) and to publish, which refuses unless
it is still origin's. Read as configured, not after `url.<base>.insteadOf`
rewriting: gh is told the GitHub repository by name, and a rewrite that
routes the push (to an SSH form of the same URL, a mirror, a test's local
repository) is the user's own setup. An origin with more than one push
URL, or one that names no `HOST/OWNER/REPO`, is refused: a local path,
a `file://` URL. An SSH host alias (`git@work:owner/repo`) gives a host
gh does not know; that fails at the first gh call with it, publish's
own `gh repo view` (pr_base, step 2), before any push.

**The environment** of the user's git: the inherited one without any
GIT_* variable (a GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE from a hook
would point git at another repository or index, and GIT_CONFIG_* would
plant config), except _TRANSPORT_VARIABLES, the ones that decide how the
user's push reaches the server and are theirs to set: credentials and SSH
(GIT_ASKPASS, GIT_SSH, GIT_SSH_COMMAND, GIT_SSH_VARIANT), TLS
(GIT_SSL_CAINFO, GIT_SSL_CAPATH, GIT_SSL_NO_VERIFY), and the transport
(GIT_HTTP_USER_AGENT, GIT_PROXY_COMMAND). None of them names a
repository, an index or config. GIT_TERMINAL_PROMPT=0 is set, so a
missing credential fails instead of waiting for a terminal nobody
watches. stdin is /dev/null.

**What publish leaves in the checkout**: the commit's objects (a tree and a
commit no ref names until git's gc removes them), and the remote-tracking
ref `refs/remotes/origin/<branch>` that git updates on a push (removed
again with a deletion). Nothing else: not the work tree, the index, HEAD
or any branch.

**Errors**: one family, PublishError, and the result. What the runner
(cm-fkd.14) records for each, and where the branch is:

| outcome          | ledger  | `pr` field        | branch on origin   | tell the user          |
|------------------|---------|-------------------|--------------------|------------------------|
| PublishRefused   | refusal | none              | never pushed       | the message: what to   |
|                  |         |                   |                    | fix                    |
| PushFailed       | failure | none              | absent, or someone | if timed_out: check    |
|                  |         |                   | else's if it       | origin for the branch  |
|                  |         |                   | appeared meanwhile | by hand                |
|                  |         |                   | (at any commit,    |                        |
|                  |         |                   | ours included)     |                        |
|                  |         |                   | (left untouched);  |                        |
|                  |         |                   | UNKNOWN if         |                        |
|                  |         |                   | `timed_out` (not   |                        |
|                  |         |                   | deleted; cm-o4j)   |                        |
| PrCreateFailed   | failure | none              | deleted if         | if not deleted: check  |
|                  |         |                   | `branch_deleted`,  | and delete it by hand  |
|                  |         |                   | else left          | (`delete_error`: why)  |
| PrOutcomeUnknown | failure | none              | left on purpose    | the PR may exist; the  |
|                  |         |                   |                    | next sweep's listing   |
|                  |         |                   |                    | finds it               |
| Published        | success | idempotence.      | the PR's head      | the PR's URL           |
|                  |         | pr_field(number,  |                    |                        |
|                  |         | branch, url, base,|                    |                        |
|                  |         | paths)            |                    |                        |

Every quote of git's or gh's output in a message is redacted and clipped
(github.quote_output).

Syntax that imports on 3.9, like git_guard.
"""

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from curamoir import git_guard, github
from curamoir.git_guard import GitGuardError, git, run_in_group, scrubbed_environment
from curamoir.guards.context import JobContext
from curamoir.guards.default_branch import DefaultBranchGuard
from curamoir.idempotence import sweep_branch_prefix

__all__ = ["CREATE_TIMEOUT", "LOOKUP_ATTEMPTS", "LOOKUP_DELAY", "PUSH_TIMEOUT",
           "REMOTE_TIMEOUT", "CreateOutputUnreadable", "Identity", "PrCreateFailed",
           "PrOutcomeUnknown", "Published", "PublishError", "PublishRefused", "PushFailed",
           "check_target", "commit_from_patch", "create_pr", "delete_remote_branch",
           "find_open_pr", "origin_gh_repo", "pr_base", "publish", "push_branch", "user_identity"]

#: Seconds `git push` may take: generous, since a pre-push hook may run tests.
PUSH_TIMEOUT = 600

#: Seconds `git ls-remote`, the branch's deletion and a config read may take.
REMOTE_TIMEOUT = 120

#: Seconds `gh pr create` may take before its outcome is unknown.
CREATE_TIMEOUT = 120

#: How many times the PR is looked for when `gh pr create`'s outcome is
#: unknown, and the seconds between two looks (module docstring, "When the
#: PR does not open"). Module attributes, so tests need not wait.
LOOKUP_ATTEMPTS = 3
LOOKUP_DELAY = 2.0

#: GIT_* variables the user's git keeps (module docstring, "The
#: environment"): how the push reaches the server, never which repository,
#: index or config git uses.
_TRANSPORT_VARIABLES = ("GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
                        "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_NO_VERIFY",
                        "GIT_HTTP_USER_AGENT", "GIT_PROXY_COMMAND")

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: An owner or repository name in a GitHub URL.
_NAME = re.compile(r"[A-Za-z0-9_.-]+")

#: scp-like `[user@]host:path`, git's form for SSH without a scheme. The
#: path may not start with `/`, which would be `host:/abs`, or a local
#: path with a colon in it.
_SCP = re.compile(r"(?:[^@/:]+@)?([^@/:]+):(?!/)(.+)")

#: URL schemes that reach a host.
_SCHEMES = ("https", "http", "ssh", "git", "git+ssh", "ssh+git")

#: The JSON fields find_open_pr asks for, when `gh pr create` failed in a
#: way that may still have opened the PR (module docstring, "When the PR
#: does not open").
_LOOKUP_FIELDS = "number,url,headRefName,headRefOid,baseRefName,isCrossRepository"

#: `--limit` for that lookup: one branch's open PRs.
_LOOKUP_LIMIT = 30


class PublishError(Exception):
    """The sweep's change could not be published (module docstring, "Errors")."""


class PublishRefused(PublishError):
    """Refused before anything was pushed: the target branch, the
    repository, the PR's base or text, the identity, the commit, or a
    branch already on origin."""


class PushFailed(PublishError):
    """`git push` failed, a pre-push hook's refusal included. `timed_out`:
    it was stopped at its bound, and whether the branch reached origin is
    unknown."""

    def __init__(self, message, timed_out=False):
        super().__init__(message)
        self.timed_out = timed_out


class PrCreateFailed(PublishError):
    """The branch was pushed but the PR did not open, and publish tried to
    delete the branch again. `create_error` is why the PR failed
    (`timed_out` if gh was stopped and no lookup found the PR);
    `branch_deleted` whether the deletion worked, and `delete_error` its
    error when it did not."""

    def __init__(self, branch, create_error, timed_out, branch_deleted, delete_error):
        cleanup = ("the branch was deleted from origin again" if branch_deleted else
                   f"deleting the branch from origin failed too ({delete_error}), so it may "
                   "still be there: check and delete it by hand")
        super().__init__(f"{branch} was pushed to origin but its PR did not open "
                         f"({create_error}); {cleanup}")
        self.branch = branch
        self.create_error = create_error
        self.timed_out = timed_out
        self.branch_deleted = branch_deleted
        self.delete_error = delete_error


class PrOutcomeUnknown(PublishError):
    """gh pr create failed in a way that may still have opened the PR (timed
    out, unreadable output, or an error GitHub may have returned after
    creating it), and looking for the PR failed. The branch is left: a
    stray branch is cheaper than closing a PR that exists."""

    def __init__(self, branch, create_error, lookup_error):
        super().__init__(f"{branch} was pushed to origin, and whether its PR opened is "
                         f"unknown: {create_error}; looking for it failed too "
                         f"({lookup_error}). The branch is left on origin, since deleting it "
                         "would close a PR that does exist.")
        self.branch = branch
        self.create_error = create_error
        self.lookup_error = lookup_error


class CreateOutputUnreadable(github.GhError):
    """`gh pr create` exited 0, but its last line is not a PR URL on the
    repository asked about: whether the PR opened is unknown. A GhError, not
    a PublishError: it is create_pr's gh-level error, beside GhTimeout, and
    publish always catches it (_open_pr), so it never escapes publish."""


@dataclass(frozen=True)
class Identity:
    """The commit's author (`name`, `email`) and committer
    (`committer_name`, `committer_email`, the author's when None)."""

    name: str
    email: str
    committer_name: Optional[str] = None
    committer_email: Optional[str] = None


@dataclass(frozen=True)
class Published:
    """The sweep's PR: the commit `sha` pushed to `branch`, opened into
    `base_branch` on `gh_repo` as PR `number` at `url`."""

    sha: str
    branch: str
    base_branch: str
    gh_repo: str
    number: int
    url: str


def publish(repo, *, base, patch, paths, branch, branch_prefix, gh_repo, message, title, body):
    """Commit `patch` (exactly `paths`) on `base`, push it to origin as
    `branch`, and open a PR from it into the default branch on `gh_repo`,
    titled `title` with `body`: a Published. The steps, and what each
    failure raises, are the module docstring's.

    Args:
        repo: the user's checkout, its root.
        base: the base commit, a full lowercase SHA (`sandbox.base`).
        patch: bytes, the gate's patch of exactly `paths`
            (green_gate.GateResult.patch).
        paths: the PR's paths (`gate.paths_for_pr`), distinct str.
        branch: sweep_pr.sweep_branch's name; `branch_prefix` the
            configured `guardrails.branch_prefix` it was made from.
        gh_repo: origin_gh_repo(repo), resolved once by the caller.
        message, title, body: str, sweep_pr's commit message, title and
            final body."""
    repo = os.path.abspath(str(repo))
    check_target(repo, branch, branch_prefix)
    origin = origin_gh_repo(repo)
    if not (isinstance(gh_repo, str) and gh_repo.casefold() == origin.casefold()):
        raise PublishRefused(f"gh would be asked about {gh_repo!r}, but origin, where the "
                             f"branch is pushed, is {origin}: the PR and the open-PR listing "
                             "must name the repository the push goes to")
    base_branch = pr_base(repo, origin, branch)
    _check_pr_text(title, body)
    identity = user_identity(repo)
    sha = commit_from_patch(repo, base, patch, message, identity, paths)
    _require_new_branch(repo, branch)
    push_branch(repo, sha, branch)
    return _open_pr(repo, sha, branch, base_branch, origin, title, body)


def check_target(repo, branch, branch_prefix):
    """PublishRefused unless `branch` is safe to write: not the default
    branch (as the checkout detects it; pr_base checks GitHub's), inside
    `branch_prefix`'s `sweep-` prefix, a valid ref name."""
    if not (isinstance(branch, str) and branch):
        # The guard judges HEAD when its target is None: never here.
        raise PublishRefused(f"the target branch {branch!r} names no branch, so nothing is "
                             "pushed")
    outcome = DefaultBranchGuard(branch_prefix, target_branch=branch)(
        JobContext(job="sweep", repo=Path(repo)))
    if outcome is not None:
        raise PublishRefused(outcome.message)
    try:
        sweep_prefix = sweep_branch_prefix(branch_prefix)
    except ValueError as error:
        raise PublishRefused(str(error)) from None
    if not branch.startswith(sweep_prefix):
        raise PublishRefused(f"branch {branch!r} is not a sweep branch ({sweep_prefix}...), "
                             "so the open-PR cap and the duplicate check would not see it")
    try:
        valid = _user_git(repo, ["check-ref-format", f"refs/heads/{branch}"], REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        # ValueError: a NUL in the name, which no argv can carry.
        raise PublishRefused(f"{branch!r} could not be checked as a branch name: "
                             f"{error}") from None
    if branch.startswith("-") or valid.returncode != 0:
        raise PublishRefused(f"{branch!r} is not a valid branch name")


def pr_base(repo, gh_repo, branch):
    """The PR's base: `gh_repo`'s default branch as GitHub names it, once the
    checkout agrees (module docstring, step 2). PublishRefused when gh cannot
    say, when it is `branch`, when origin/HEAD names another branch, or when
    the checkout has no remote-tracking ref for it."""
    try:
        printed = github.run_gh(["repo", "view", gh_repo, "--json", "defaultBranchRef"],
                                cwd=repo)
    except github.GhError as error:
        raise PublishRefused(f"gh could not say which branch is {gh_repo}'s default "
                             f"({error}), so the PR has no base; nothing was pushed") from None
    try:
        default = json.loads(printed)["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        default = None
    if not (isinstance(default, str) and default):
        raise PublishRefused(f"gh repo view printed {github.quote_output(printed)}, not "
                             f"{gh_repo}'s default branch, so the PR has no base")
    if default == branch:
        raise PublishRefused(f"refusing to write on the default branch {default!r}")
    tracking = f"refs/remotes/origin/{default}"
    try:
        head = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD",
                   ok=(0, 1, 128)).decode("utf-8", "replace").strip()
        known = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", tracking,
                    ok=(0, 1, 128)).strip()
    except GitGuardError as error:
        raise PublishRefused(f"could not read the checkout's view of origin: {error}") from None
    if head and head != tracking:
        raise PublishRefused(f"GitHub says {gh_repo}'s default branch is {default!r}, but the "
                             f"checkout's refs/remotes/origin/HEAD names "
                             f"{head.removeprefix('refs/remotes/origin/')!r}, which is stale "
                             "(the default was changed since): run `git remote set-head "
                             "origin -a` and `git fetch origin`, then sweep again")
    if not known:
        raise PublishRefused(f"GitHub says {gh_repo}'s default branch is {default!r}, but the "
                             f"checkout has no {tracking}: run `git fetch origin` (and `git "
                             "remote set-head origin -a`), then sweep again")
    return default


def _check_pr_text(title, body):
    """PublishRefused unless gh can be given `title` and `body` (module
    docstring, step 2): str that encode as UTF-8, and no NUL in the title,
    which travels as an argument."""
    for name, text in (("title", title), ("body", body)):
        if not isinstance(text, str):
            raise PublishRefused(f"the PR's {name} must be text, not {type(text).__name__}")
        try:
            text.encode("utf-8")
        except UnicodeError as error:
            raise PublishRefused(f"the PR's {name} cannot be sent ({error}), so nothing "
                                 "is pushed") from None
    if "\0" in title:
        raise PublishRefused("the PR's title holds a NUL, which no argument can carry, so "
                             "nothing is pushed")


def origin_gh_repo(repo):
    """origin's GitHub repository as gh's `--repo` takes it,
    `HOST/OWNER/REPO` (module docstring, "Which repository"), read from
    origin's push URL as configured. PublishRefused when there is no origin,
    more than one push URL, or a URL that names no GitHub repository."""
    urls = _config_all(repo, "remote.origin.pushurl") or _config_all(repo, "remote.origin.url")
    if not urls:
        raise PublishRefused("the checkout has no origin remote, so there is nowhere to push "
                             "the sweep's branch")
    if len(urls) > 1:
        raise PublishRefused(f"origin has more than one push URL ({len(urls)}), so a push "
                             "would go to several repositories and the PR to one")
    named = _github_repo(urls[0])
    if named is None:
        raise PublishRefused(f"origin's URL {github.quote_output(urls[0])} is not a GitHub "
                             "repository URL (HOST/OWNER/REPO), so gh cannot be told which "
                             "repository the PR belongs to")
    return named


def user_identity(repo):
    """The commit's Identity from the checkout's git config, with git's
    precedence (module docstring, step 3): `author.*`, else `user.*`, for
    the author; `committer.*`, else `user.*`, for the committer.
    PublishRefused if a name or email has neither."""
    def value(key):
        values = _config_all(repo, key)
        return values[-1] if values and values[-1].strip() else None

    user = {field: value(f"user.{field}") for field in ("name", "email")}
    found = {}
    for role in ("author", "committer"):
        for field in ("name", "email"):
            chosen = value(f"{role}.{field}") or user[field]
            if chosen is None:
                raise PublishRefused(f"the checkout's git config has no user.{field} (nor "
                                     f"{role}.{field}), so the sweep's commit would have no "
                                     f"{role}: set it with `git config`")
            found[f"{role}.{field}"] = chosen
    return Identity(found["author.name"], found["author.email"], found["committer.name"],
                    found["committer.email"])


def commit_from_patch(repo, base, patch, message, identity, paths):
    """The SHA of a commit whose parent is `base` and whose tree is `base`'s
    with `patch` applied, by `identity`, with `message`, built in `repo`'s
    object store through a temporary index (module docstring, step 4).
    PublishRefused if it cannot be built or it changes anything but exactly
    `paths`."""
    if not (isinstance(base, str) and _COMMIT.fullmatch(base)):
        raise PublishRefused(f"the base must be a full commit SHA, not {base!r}")
    if not patch or not paths:
        raise PublishRefused("there is no change to commit: the patch or its paths are empty")
    wanted = set(paths)
    if len(wanted) != len(paths) or not all(isinstance(p, str) and p for p in wanted):
        raise PublishRefused("the PR's paths must be distinct non-empty strings")
    with tempfile.TemporaryDirectory(prefix="curamoir-commit-") as scratch:
        index = os.path.join(scratch, "index")
        patch_file = os.path.join(scratch, "patch")
        message_file = os.path.join(scratch, "message")
        try:
            Path(patch_file).write_bytes(patch)
            Path(message_file).write_text(message, encoding="utf-8")
        except (OSError, UnicodeError, TypeError) as error:
            raise PublishRefused(f"the patch or the commit message could not be written for "
                                 f"git ({error}), so no commit is made") from None
        with_index = {"GIT_INDEX_FILE": index}
        author = {"GIT_AUTHOR_NAME": identity.name, "GIT_AUTHOR_EMAIL": identity.email,
                  "GIT_COMMITTER_NAME": identity.committer_name or identity.name,
                  "GIT_COMMITTER_EMAIL": identity.committer_email or identity.email}
        try:
            git(repo, "read-tree", base, extra_env=with_index)
            git(repo, "apply", "--cached", "--whitespace=nowarn", patch_file,
                extra_env=with_index)
            tree = git(repo, "write-tree", extra_env=with_index).decode("ascii").strip()
            sha = git(repo, "commit-tree", "--no-gpg-sign", tree, "-p", base, "-F",
                      message_file, extra_env=author).decode("ascii").strip()
            changed = git(repo, "diff-tree", "-r", "-z", "--name-only", "--no-renames",
                          base, sha)
        except GitGuardError as error:
            raise PublishRefused(f"the sweep's commit could not be built: {error}") from None
    names = {name.decode("utf-8", "surrogateescape") for name in changed.split(b"\0") if name}
    if names != wanted:
        extra, missing = sorted(names - wanted), sorted(wanted - names)
        raise PublishRefused(f"the commit changes not exactly the PR's paths (also: {extra}, "
                             f"missing: {missing}), so it is not pushed")
    return sha


def push_branch(repo, sha, branch):
    """`git push origin <sha>:refs/heads/<branch>` from the checkout, with
    the user's git, leased on the branch not existing (module docstring,
    step 6). PushFailed unless origin reports a new branch."""
    destination = f"refs/heads/{branch}"
    appeared = (f"{branch} appeared on origin after it was checked, so it was not pushed "
                "and is left as it is")
    pushed = _push(repo, [f"--force-with-lease={destination}:", "--no-follow-tags",
                          "--recurse-submodules=no", "origin", f"{sha}:{destination}"],
                   PUSH_TIMEOUT, f"pushing {branch}", stale=appeared)
    # Only `*` means this push created the branch. git checks "up to date"
    # before the lease: if the branch appeared on origin at this very commit
    # (commit-tree is deterministic, so another publish of the same base in
    # the same second makes the same SHA), the push prints `=` [up to date]
    # and exits 0. That branch is someone else's: no PR is opened on it.
    flag = _flag(pushed, f"{sha}:{destination}")
    if flag == "=":
        raise PushFailed(f"pushing {branch}: {appeared}")
    if flag != "*":
        raise PushFailed(f"origin did not report {branch} as a new branch: "
                         f"{github.quote_output(pushed)}")


def delete_remote_branch(repo, branch, sha):
    """`git push origin --delete refs/heads/<branch>` from the checkout,
    leased to `sha`: a branch that no longer points at it is left alone
    (module docstring, "When the PR does not open"). PushFailed unless
    origin reports it deleted."""
    destination = f"refs/heads/{branch}"
    deleted = _push(repo, [f"--force-with-lease={destination}:{sha}", "origin", "--delete",
                           destination], REMOTE_TIMEOUT, f"deleting {branch}",
                    stale=f"{branch} on origin no longer points at {sha} (moved or deleted "
                          "since the push), so it was not deleted")
    # Defence in depth, not reachable today: leased to `sha`, a deletion that
    # exits 0 removed the ref (`-`); a ref that is absent or elsewhere fails
    # the lease (exit 1).
    if _flag(deleted, f":{destination}") != "-":
        raise PushFailed(f"origin did not report {branch} deleted: "
                         f"{github.quote_output(deleted)}")


def create_pr(repo, *, gh_repo, branch, base_branch, title, body):
    """`gh pr create` from `branch` into `base_branch` on `gh_repo`, the body
    read from a temporary file (module docstring, step 7): (number, url).
    GhError if gh fails: GhTimeout when stopped, CreateOutputUnreadable
    when it exits 0 but prints no PR URL on `gh_repo` (both: the outcome
    is unknown)."""
    handle, body_file = tempfile.mkstemp(prefix="curamoir-pr-body-", suffix=".md")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as written:
            written.write(body)
        printed = github.run_gh(["pr", "create", "--repo", gh_repo, "--head", branch,
                                 "--base", base_branch, "--title", title,
                                 "--body-file", body_file], cwd=repo, timeout=CREATE_TIMEOUT)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(body_file)
    lines = [line.strip() for line in printed.splitlines() if line.strip()]
    number = _pr_number(lines[-1], gh_repo) if lines else None
    if number is None:
        raise CreateOutputUnreadable(f"gh pr create printed {github.quote_output(printed)!s}, "
                                     f"not the URL of a pull request on {gh_repo}")
    return number, lines[-1]


def find_open_pr(repo, gh_repo, branch, sha, base_branch):
    """The open same-repository PR from `branch` on `gh_repo` whose head is
    `sha` and whose base is `base_branch`, as (number, url), or None (a PR
    from that branch name of another commit or base is not the sweep's).
    GhError if gh fails or lists more than one."""
    printed = github.run_gh(["pr", "list", "--repo", gh_repo, "--state", "open", "--head",
                             branch, "--limit", str(_LOOKUP_LIMIT), "--json", _LOOKUP_FIELDS],
                            cwd=repo)
    try:
        items = json.loads(printed)
    except ValueError:
        items = None
    if not isinstance(items, list):
        raise github.GhError(f"gh pr list printed {github.quote_output(printed)}, not a list "
                             "of pull requests")
    found = []
    for item in items:
        if not (isinstance(item, dict) and item.get("headRefName") == branch
                and item.get("isCrossRepository") is False
                and item.get("headRefOid") == sha and item.get("baseRefName") == base_branch):
            continue
        number = _pr_number(item.get("url"), gh_repo)
        if number is None or number != item.get("number"):
            raise github.GhError(f"gh listed a PR from {branch} whose number and URL do not "
                                 f"agree: {github.quote_output(json.dumps(item))}")
        found.append((number, item["url"]))
    if len(found) > 1:
        raise github.GhError(f"gh listed {len(found)} open PRs from {branch}")
    return found[0] if found else None


# --- the steps' parts ----------------------------------------------------------------

def _open_pr(repo, sha, branch, base_branch, gh_repo, title, body):
    """The PR, or the error, as the module docstring's "When the PR does not
    open" table decides."""
    try:
        number, url = create_pr(repo, gh_repo=gh_repo, branch=branch,
                                base_branch=base_branch, title=title, body=body)
    except (github.GhTimeout, CreateOutputUnreadable) as error:
        found = _find_pr_after_create(repo, gh_repo, branch, sha, base_branch, error,
                                      LOOKUP_ATTEMPTS)
        if found is None:
            raise _delete_branch_and_fail(
                repo, branch, sha, f"{error}, and no PR from it was found",
                timed_out=isinstance(error, github.GhTimeout)) from None
        number, url = found
    except github.GhError as error:
        # GitHub may have made the PR and then answered with an error.
        found = _find_pr_after_create(repo, gh_repo, branch, sha, base_branch, error, 1)
        if found is None:
            raise _delete_branch_and_fail(repo, branch, sha, str(error),
                                          timed_out=False) from None
        number, url = found
    except (OSError, ValueError) as error:
        # Not gh's: the body file, a lone surrogate, a NUL. gh never ran
        # with it, so there is no PR.
        raise _delete_branch_and_fail(repo, branch, sha,
                                      f"the PR could not be asked for: {error}",
                                      timed_out=False) from None
    return Published(sha, branch, base_branch, gh_repo, number, url)


def _find_pr_after_create(repo, gh_repo, branch, sha, base_branch, create_error, attempts):
    """find_open_pr up to `attempts` times, LOOKUP_DELAY seconds apart,
    after `create_error` (what gh pr create raised): the PR, or None when no
    attempt found it. PrOutcomeUnknown as soon as a lookup fails."""
    for attempt in range(attempts):
        if attempt:
            time.sleep(LOOKUP_DELAY)
        try:
            found = find_open_pr(repo, gh_repo, branch, sha, base_branch)
        except github.GhError as lookup:
            raise PrOutcomeUnknown(branch, str(create_error), str(lookup)) from None
        if found is not None:
            return found
    return None


def _delete_branch_and_fail(repo, branch, sha, create_error, timed_out):
    """Delete the pushed branch (leased to `sha`) and return the
    PrCreateFailed carrying both outcomes, for the caller to raise."""
    try:
        delete_remote_branch(repo, branch, sha)
    except PushFailed as error:
        return PrCreateFailed(branch, create_error, timed_out, False, str(error))
    return PrCreateFailed(branch, create_error, timed_out, True, None)


def _require_new_branch(repo, branch):
    destination = f"refs/heads/{branch}"
    try:
        target = _user_git(repo, ["remote", "get-url", "--push", "origin"], REMOTE_TIMEOUT)
        if target.returncode != 0:
            raise PublishRefused(f"could not read origin's push URL: {_stderr(target)}")
        push_url = target.stdout.decode("utf-8", "surrogateescape").strip()
        listed = _user_git(repo, ["ls-remote", "--end-of-options", push_url, destination],
                           REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise PublishRefused(f"could not ask origin whether {branch} exists: {error}") from None
    if listed.returncode != 0:
        raise PublishRefused(f"could not ask origin whether {branch} exists: "
                             f"{_stderr(listed)}")
    refs = [line.split("\t", 1)[-1] for line in listed.stdout.decode("utf-8", "replace")
            .splitlines()]
    if destination in refs:
        raise PublishRefused(f"{branch} already exists on origin; it is left as it is (it "
                             "may carry an open sweep PR)")


def _push(repo, args, timeout, doing, stale):
    """The stdout of `git push --porcelain <args>` with the user's git;
    PushFailed if it fails or is stopped at `timeout`, with `stale` as the
    message when a lease refused it (`[rejected] (stale info)`)."""
    try:
        done = _user_git(repo, ["push", "--porcelain", *args], timeout)
    except subprocess.TimeoutExpired:
        raise PushFailed(f"{doing}: `git push` did not finish within {timeout} second(s) and "
                         "was stopped, so whether origin has the change is unknown",
                         timed_out=True) from None
    except OSError as error:
        raise PushFailed(f"{doing}: git could not be started ({error})") from None
    if done.returncode != 0:
        said = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
        # The lease's refusal, or origin's own when the ref appeared while
        # the push was under way: the same thing to the user.
        if "(stale info)" in said or "reference already exists" in said:
            raise PushFailed(f"{doing}: {stale}")
        raise PushFailed(f"{doing}: `git push` exited {done.returncode}: {_stderr(done)}")
    return done.stdout.decode("utf-8", "replace")


def _flag(porcelain, refspec):
    """The flag `git push --porcelain` printed for `refspec`, or None."""
    for line in porcelain.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and len(fields[0]) == 1 and fields[1] == refspec:
            return fields[0]
    return None


def _user_git(repo, args, timeout):
    """`git args` in `repo` with the user's own config (module docstring,
    "The environment"): a CompletedProcess with bytes output."""
    environment = scrubbed_environment()
    environment.update({name: os.environ[name] for name in _TRANSPORT_VARIABLES
                        if name in os.environ})
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return run_in_group([git_guard.GIT, *args], cwd=str(repo), env=environment,
                        timeout=timeout)


def _config_all(repo, key):
    """Every value of `key` in the checkout's git config, in order; [] if unset."""
    try:
        done = _user_git(repo, ["config", "--get-all", key], REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise PublishRefused(f"could not read {key} from the checkout's git config: "
                             f"{error}") from None
    if done.returncode == 1:
        return []
    if done.returncode != 0:
        raise PublishRefused(f"could not read {key} from the checkout's git config: "
                             f"{_stderr(done)}")
    return [line for line in done.stdout.decode("utf-8", "replace").split("\n") if line]


def _github_repo(url):
    """`host/owner/name` for a GitHub repository URL, else None."""
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _SCHEMES:
            return None
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        # gh names a GitHub Enterprise server on another HTTPS port as
        # HOST:PORT; an SSH port is the transport's, not the host's name.
        default = {"https": 443, "http": 80}.get(parts.scheme.lower())
        if host and default is not None and port not in (None, default):
            host = f"{host}:{port}"
        path = parts.path
    else:
        match = _SCP.fullmatch(url)
        if match is None:
            return None
        host, path = match.groups()
    segments = path.strip("/").split("/")
    if not host or len(segments) != 2:
        return None
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not all(_NAME.fullmatch(part) and part not in (".", "..") for part in (owner, name)):
        return None
    return f"{host.lower()}/{owner}/{name}"


def _pr_number(url, gh_repo):
    """The PR number in `url` when it is a PR URL on `gh_repo`, else None."""
    host, owner, name = [*gh_repo.split("/"), "", "", ""][:3]
    pattern = (rf"https://{re.escape(host)}/{re.escape(owner)}/{re.escape(name)}"
               r"/pull/([1-9][0-9]*)")
    match = re.fullmatch(pattern, url, re.IGNORECASE) if isinstance(url, str) else None
    return int(match.group(1)) if match else None


def _stderr(done):
    return github.quote_output(done.stderr.decode("utf-8", "replace")) or "no message"
