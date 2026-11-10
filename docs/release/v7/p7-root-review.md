# P7 root acceptance review

Status: **needs work**. The production wiring and canonical candidate/event
boundaries are present, but the following defects prevent bounded lifecycle
acceptance.

1. **High: eligible records are permanently starved.** Both failed and pending
   analysis slice `eligible[:per_session]` before excluding records with recent
   candidates. Three failed decisions, a per-session limit of one, and three
   passes produce only one candidate. Every later pass sees the same recently
   reviewed first record. In addition, `_records` always reads the latest 1,000
   records; older eligible decisions can never be visited. The persisted cursor
   is written to the latest event but never consumed for scheduling. Use fair,
   resumable bounded source selection, with cooldown filtering before selecting
   work and restart coverage beyond a page.

2. **High: foreground activity can be discarded after waiting for capacity.**
   `run_once` waits on the global semaphore, then unconditionally clears the
   cancellation event without rechecking `foreground_calls` or activity epoch.
   A queued workspace that receives a foreground request starts all four
   strategies while that request remains active. Recheck eligibility after
   capacity acquisition and immediately before writes; retain cancellation
   during already admitted work.

3. **High: analysis lacks effective byte/work bounds and cancellation.**
   `_records` loads 1,000 complete contents before checking cancellation (up to
   100 million characters through the public schema, potentially larger migrated
   contents). Connection discovery scans all generation memberships and all
   active relationships without limits or cancellation, materializes matching
   memberships and pair candidates, and only checks cancellation when producing
   final proposals. Add explicit byte/row/pair budgets, bounded SQL selection,
   cooperative cancellation during scans/ranking, and truthful incomplete-work
   state so large workspaces converge across slices. Do not silently summarize
   unchecked/truncated evidence as unanimous when automatic outcomes are enabled.

Independent reproduction: `.tmp/p7_review_probe.py` prints three eligible
decisions/three passes/one candidate and one foreground call/four strategies
started. Initial execution also hit a probe-owned SQLite cleanup handle; this
was a probe cleanup issue and is not counted as a production finding.

Required regression evidence includes cooldown starvation, restart beyond the
source page, queued foreground arrival, cancellation during source/graph scans,
large-content byte admission, and bounded shutdown with no post-cancel write.
