# sample

A deliberately imperfect repository. It exists so cúramóir's acceptance tests
have something real to act on: planted principle violations, planted bugs, a
quickstart that can be broken on purpose, and a few beads.

**Do not "fix" anything here.** Every flaw is load-bearing and recorded in
`PLANTED.json`, which names the line it lives on and the acceptance criterion
that needs it. Reformatting this repo moves those lines and breaks tests in
other epics for reasons unrelated to what they are testing.
