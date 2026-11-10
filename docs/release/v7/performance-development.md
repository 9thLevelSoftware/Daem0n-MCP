# v7 scale development evidence

Run date: 2026-09-17. These measurements are from the uncommitted completion
checkout, not a final release commit or a certified reference machine.

## Fixture and machine

`benchmarks/v7_scale.py` creates synthetic records through `EventStore` and its
canonical projections. The fixture has 100,000 memories and ten authoritative
revisions per memory, for one million events. It uses eight topic families,
four record categories, and deterministic record identities. It does not replace
the frozen relevance judgments or establish retrieval quality.

The Windows 11 machine has an Intel Core i9-14900HX, 24 physical cores,
32 logical processors, and 33,957,072,896 bytes of physical memory. This is not
the proposed eight-core reference machine.

It is also a shared host. Development snapshots ranged from about 4.9 GB
available memory to only 414 MB (98.8% in use, 96.1% CPU), despite 32 GB
installed. Other user workloads were left running. See
`.tmp/performance/host-load-snapshot.json`; unloaded
reference-machine certification cannot be inferred from these timings.

```text
.tmp/venv312/Scripts/python.exe -m benchmarks.v7_scale seed .tmp/performance/scale100k-1m --records 100000 --versions 10
```

The canonical seed completed in 762.79 seconds. Initial lexical indexing took
7.19 seconds. The checkpointed database was 2,501,681,152 bytes. Seed generation
is not a v6 migration measurement. Evidence: `.tmp/scale-seed100k.log` and the
fixture's `scale-fixture.json`.

## Integration finding

The first actual stdio recall probe did not pass. Lexical search returned
50 candidates in approximately 46–65 milliseconds, but the final result
abstained with `POLICY_STATE_UNAVAILABLE`. An exact component-name query also
returned one lexical candidate and then the same abstention. This is a release
blocker; empty results are not counted as fast successful recall.

Evidence: `.tmp/scale100k-integration-probe.log` and
`.tmp/scale100k-recall-debug.log`. The cause was rehashing one million event
hashes for every policy and selected-content read, exceeding the repository
deadline. A generation-owned, revision-observed root cache restores successful
recall while invalidating on external database commits.
The probe overlapped integration tests, so its timings are diagnostic only.

One subsequent stdio development run measured 12 successful recalls at four-way
concurrency: p50 298 ms and p95/max 440 ms, startup 3.64 seconds. Evidence:
`.tmp/scale100k-repository-cache-final.log`. Source files changed during that
run, so it is not a certification result. New-write lexical visibility was
18.09 seconds and specialized visibility 34.79 seconds. Transactional lexical
updates subsequently reduced lexical visibility to 5.40 seconds, still above
the five-second target. Full rebuilds contend with the first post-write
integrity scan; a bounded batching delay is under test. These changes await
independent review and stable-source measurements.

The benchmark now measures four concurrent real MCP recalls and one authorized
new write followed by lexical and specialized-projection visibility checks.
A 100-record smoke fixture passed those paths; its results are not scale
certification. Source fingerprints and a changed-during-run indicator accompany
successful measurement output.

`benchmarks/v7_soak.py` exercises actual concurrent recalls, authorized writes
and outcomes, briefing renewal, health and resource reads, with bounded timing
samples and process resource measurements. An 18-second fresh-workspace smoke
completed 160 recalls and clean shutdown (`.tmp/soak-smoke-fresh-fixed.log`).
The eight-hour run has not started.
The soak runner additionally requires `psutil`; install it in the benchmark
environment with `python -m pip install psutil` (it is not a core dependency).

## Open gates

Successful 100,000-memory lexical/hybrid latency and visibility, real dense
indexing, migration timing, cold model loading, relevance comparisons, stress
measurements, and the eight-hour mixed-workload soak remain open. No performance
release gate is accepted by this report.
