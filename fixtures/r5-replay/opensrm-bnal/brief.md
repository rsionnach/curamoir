# opensrm-bnal: eco-doctor: DUPLICATE-NAME fires on any sibling worktree, blocking every gate

Found on the FIRST real use of the opensrm-px23 pre-flight, gating opensrm-ocvu.

  DUPLICATE-NAME   (workspace)   nthlayer-common declared by nthlayer-common,
                                 nthlayer-common-ocvu-target-units

A git worktree declares the same project.name as its parent checkout, always.
CLAUDE.md MANDATES sibling worktrees for parallel work
(.claude/bin/eco-worktree.sh), so DUPLICATE-NAME fires whenever anyone works the
prescribed way — and px23 classifies it as a RANGE finding, which aborts the
gate. The R5 gate therefore refuses to run in the workspace's normal operating
mode.

This is the exact failure px23's own text warns about for BEHIND: 'branching on
the exit code would refuse every gate in the workspace whenever an unrelated
repo happened to be behind'. I avoided it for BEHIND and recreated it for
DUPLICATE-NAME.

WHY THE CLASSIFICATION IS WRONG. DUPLICATE-NAME was added in opensrm-8hn3 as a
safety net beneath NAME-BASED sibling resolution. But the same bead replaced
name-based resolution with [tool.uv.sources] path resolution — so the net now
sits beneath a mechanism that does not use names. It reports an ambiguity that
no longer affects any decision the tool makes.

FIX, preferred: teach eco-doctor that a worktree of the SAME repo is not a
duplicate. Two directories sharing a git-common-dir are one package checked out
twice, which is expected; two DIFFERENT repos claiming one name is the genuine
ambiguity worth reporting. Key on .git, exactly as
.claude/hooks/r5-lock.sh already does to make the supervisor lock per-repo.

  same common-dir   -> not a finding
  different repos   -> DUPLICATE-NAME, stays blocking

Alternative if that proves awkward: reclassify DUPLICATE-NAME as advisory in the
px23 pre-flight. Weaker — it silences a genuine duplicate too.

TEST: nthlayer/test/test_eco_doctor.sh currently builds its duplicate fixture as
two independent Reinitialized existing Git repository in <workspace>/opensrm/.git/ repos, which is why this was not caught — that
fixture is the 'different repos' case, and it should stay. Add a second fixture
using a real  so both branches are covered. The existing Test 9
proves the shadowing protection still works; this adds the case that
distinguishes a worktree from a rival.

WORKAROUND UNTIL FIXED: the finding is a diagnosed false positive when the
duplicate is a worktree of the repo under gate. Verify that is the case before
proceeding — a worktree of a DIFFERENT repo, or two genuinely distinct
checkouts, is a real finding.

Blocks nothing structurally, but it blocks every gate in practice, so P1.
