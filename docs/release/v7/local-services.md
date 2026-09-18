# Local certification services

These are test dependencies, not release acceptance results. Started 2026-09-16 under Docker Desktop 29.7.2 on Windows. Existing unrelated containers were left untouched.

| Dependency | Local endpoint | Verification |
| --- | --- | --- |
| Valkey (image valkey/valkey:8) | 127.0.0.1:16379 | authenticated PING passed; unauthenticated rejected; AOF enabled with appendfsync always |
| Qdrant 1.19.1 | 127.0.0.1:16333 | authenticated /collections 200; unauthenticated 401 |

Owned container/volume names: `daem0n-v7-valkey-certification`, `daem0n-v7-qdrant-certification`.

Image digests:
- Valkey: `sha256:3fbd2e3e4b6e85e046c1e7c215e8f79087bc0357789184305806664e320996f3`
- Qdrant: `sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10`

Credentials are in ignored `.tmp/certification-secrets/` files, protected by a current-user/SYSTEM directory ACL. Never copy those files into release artifacts, task payloads, or logs. Only loopback ports are published. Persistent Docker volumes retain test state for restart tests.

Installed local clients: Claude Code 2.1.274; OpenCode 1.18.21. Native-edit workflow certification has not yet run. No E2B staging credential is available in the checked environment. Authenticated remote deployment and supported-platform performance/soak gates remain pending.
