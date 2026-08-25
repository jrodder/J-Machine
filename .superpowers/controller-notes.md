# Controller session protocol — rescued 2026-08-25 before SDD workspace deletion
# Source: .superpowers/sdd/2026-08-24-reticulum-game-host/progress.md, "Resume record" section (user-marked "(any session, durable)")
# Keep: shared-ninfer context-wall forensics + dispatch discipline for future J-Machine controller sessions.

Resume record (2026-08-24, new controller session after previous session died):
- Previous controller session died at 191,596 tokens: final assistant turn truncated
  by server length cap (stopReason=length) mid tool-call → unresumable. Nothing lost:
  state = this ledger + plan + briefs + report files + git commits. Task 4's prior
  forked dispatch died at 156,545 tokens (== parent session size, prefill aborted);
  its fresh re-dispatch (cacd660d) died at 35 s on admission, 0 tokens, 0 work.
  Task 4 state: NOT STARTED; BASE addd44f.
- Root cause (verified 2026-08-24, refined after the 131k over-correction — forensics
  below): the declared window 196500 MATCHES the model's real window (~196,608; Phase 1
  session ran 19 h at ~196k, surviving 26 hard-limit overflow-recovery compactions).
  The observed wall is ~165k total context IN FLIGHT across the shared ninfer server
  (user ruling + GPU KV 31.7/32.6 GiB resident at idle): a single 189k prefill worked;
  a forked child's 156,545-token prefill aborted WHILE the parent held ~150k
  (concurrent KV, not a single-request wall); children dispatched mid-parent-generation
  expire in admission. Two pi mechanics compound this (verified in dist source):
  (1) auto-compaction is checked ONLY at run boundaries (agent_end / before user prompt
  — agent-session.js _handlePostAgentRun + pre-prompt check), never mid-run, so one long
  autonomous turn can blow past the threshold unchecked; (2) even when triggered (dead
  session: pre-prompt check at 178,308 > old threshold 159,636 fired), the
  summarization call is itself a large prefill that failed on the saturated server,
  and pi proceeds with the user prompt anyway (_checkCompaction returns false; caller
  ignores it) — compaction is a best-effort backstop, not a guarantee. Subagents
  monitor nothing; builtin `worker` defaults to context:fork (the 156,545 abort), so
  every dispatch must pass context:"fresh" explicitly.
- Fix (config only, no code; corrected 2026-08-24 after user ruling — the model has a
  197k total context; keep in-flight context at ~165k): models.json contextWindow
  196500 (restored — it was right), maxTokens 32000 (restored; 197k = 165k + 32k);
  settings.json compaction {enabled:true, reserveTokens:31500, keepRecentTokens:20000}
  → auto-compaction fires at exactly 165,000. (Earlier over-correction to
  131072/16384 — based on misreading the concurrent-KV abort as a single-request wall
  — reverted.) The durable guard is NOT the compaction trigger — it is the session
  discipline below.
- Resume protocol (any session, durable): read this ledger from the top of the current
  task + the plan's Global Constraints; the controller session is disposable — a fresh
  session resumes from the last ledger entry with zero lost state. Children:
  context:"fresh" only (never fork from a bloated parent). Dispatch a child, then END
  THE TURN — children dispatched while the parent is mid-generation on the shared local
  server expire in admission (observed: 4 admission timeouts + cacd660d), and a
  concurrent large controller prefill + child prefill exceeds the ~165k in-flight
  budget. Controller checks its own session totalTokens (last usage in session file)
  at each task boundary; if > ~140k, finish the boundary cleanly and start a fresh
  session for the next task (compaction's 165k trigger is a backstop, not a plan — it
  is not checked mid-run and its summarization call can fail under saturation).
  Keep controller tool outputs small (head/grep, never cat large files into context)
  — one long turn can grow the context well past the compaction trigger unchecked.
  REFINEMENT (2026-08-24, 5th admission timeout observed, reviewer fbfec7ac): the
  dispatch must be the LAST tool call of the turn — nothing after it, not even a
  session-size check. The fbfec7ac child's 2nd request expired in admission while the
  controller's post-dispatch size-check turn was still generating on the shared server
  (child req 1 succeeded at 6.3k input; req 2 died 37s in). Revive failed runs with
  the agent + id (action:resume equivalent) at a true turn boundary — reviving
  preserves the child's completed requests (e.g. the diff read) and halves the
  prefill load.

