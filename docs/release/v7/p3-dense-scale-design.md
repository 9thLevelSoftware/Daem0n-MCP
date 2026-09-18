# P3 dense 100k generation design

Date: 2026-09-17

Status: **architecture recommendation; implementation and scale acceptance are
open**.

## Decision

Implement a checksum-attested reuse path from the prior active Qdrant
generation, plus bounded inference and provider batches. Preserve the current
generation-isolated collection, complete-set validation, source-snapshot
revalidation, and atomic manifest activation.

Do not update the active collection in place. A failed or superseded build must
leave the previous generation readable. Do not treat a vector as reusable only
because it has the right dimension and finite values. A reusable vector must
have been checked against a local encoder result in an earlier local build and
must still match its SQLite-held attestation.

Use schema version **31** for this work. Version 30 is already assigned to the
separate P3 scale-index migration.

## Evidence and present bottleneck

`DenseProjectionBuilder.rebuild()` currently materializes every point by
calling `encoder.encode(record.content)` once per live record. The recent
128-point change bounds Qdrant upsert and retrieve calls, but it does not batch
model inference. `_active_is_current()` also recreates every expected point, so
even an unchanged-generation validation re-embeds the complete corpus.

The configured Nomic ONNX encoder and authenticated Qdrant built 257 records in
21.658 seconds (`.tmp/dense257-real.log`). That duration includes model load and
the run observed a changing source, so it is integration evidence rather than a
rate benchmark. A deliberately crude linear extrapolation is about 8,427
seconds (2.34 hours) for 100,000 scalar encodes. Fixed model-load cost makes that
extrapolation imprecise, but it is sufficient to reject the claim that provider
batching alone plausibly establishes the five-minute post-write requirement.
Inference batching may improve the cold build substantially, but continuing to
embed all 100,000 records after every write has no measured basis for a
five-minute guarantee.

The existing durable job coalescing, source event count/root, staging manifest,
generation-specific collection, and activation-time source validation are the
right authority boundaries. The repair should reuse them rather than introduce
an independent incremental authority.

## Attested reuse contract

Migration 31 should add nullable `vector_format` and `vector_sha256` columns to
`dense_projection_refs`. The accepted format is a single versioned value such
as `qdrant-cosine-f32-le-v1`; the checksum is lowercase SHA-256. Both columns
must be null or both non-null. Old and portable-import refs migrate as null and
are therefore ineligible for reuse.

Each locally built manifest should also contain:

- `encoder_artifact_fingerprint`, identifying the exact model and tokenizer
  artifacts used by the document encoder; and
- `vector_space_hash`, a canonical hash over the artifact fingerprint,
  encoder contract, model ID, dimension, distance, builder version, and vector
  format. It deliberately excludes collection name and generation.

`model_id` is insufficient for persistent reuse because a remote model name or
local directory can resolve to different bytes later. For the configured ONNX
path, expose the resolved Hugging Face snapshot commit and hashes of the ONNX
graph and tokenizer inputs. For a local model directory, hash a sorted,
path-normalized manifest of the regular artifact files and reject linked or
changing inputs. An encoder that cannot provide a stable artifact fingerprint
remains usable for a full build but is not eligible for persistent reuse.

After uploading a locally encoded point, retain the current semantic check:
the returned Qdrant vector must match the local vector by the bounded cosine
comparison. Then encode the returned provider representation as finite,
non-zero little-endian float32 components, canonicalizing negative zero, and
store a domain-separated SHA-256 over:

```
format, workspace_id, provider_key, vector_space_hash,
record_id, content_hash, source_event_id, dimension, vector_bytes
```

The checksum is an integrity and provenance attestation relative to the trusted
SQLite event database. It is not a signature against an attacker who can
rewrite that database and recompute checksums; such an attacker already controls
the canonical event authority.

For a later generation, a record is reusable only when all of the following
match the prior active generation:

1. workspace, provider, record ID, content hash, source event ID, model, and
   dimension;
2. the complete stable `vector_space_hash`;
3. a ready prior ref with the supported vector format and checksum;
4. the deterministic point ID and exact prior-generation payload; and
5. the recomputed checksum of the retrieved prior Qdrant vector.

A content, provenance, payload, vector, checksum, or contract mismatch is a
cache miss for that record and invokes the local encoder. A provider transport
failure aborts the attempt for durable retry instead of silently turning a
transient outage into an unbounded full re-encode. This chain makes prior
provider vectors useful without trusting provider storage by itself.

The new collection still receives one point for every current record, with its
new generation payload. After upload, validate every point and store the
checksum of the newly returned representation on the pending ref. Activation
requires every local-build ref to have a valid checksum and repeats the current
canonical source and manifest checks. If the source changed, cleanup removes
the staging refs and collection while the old active generation remains intact.
Checksums written to a failed staging generation never become reuse inputs.

## Bounded build flow

Add `encode_many()` to `ConfiguredEmbeddingEncoder` and the pooled ONNX adapter.
It should prefix inputs, invoke the backend with a bounded list, hold the model
lock for one backend call, preserve input order, and validate the dimension and
finiteness of every output. Keep `encode()` as the one-item compatibility path.
Choose the initial inference batch bound from measured memory use; do not assume
that the provider batch size of 128 is also safe for 8,192-token transformer
inputs.

Refactor dense generation into bounded record batches:

1. resolve and verify eligible vectors from the prior active collection;
2. batch-encode only misses;
3. upsert the complete batch into the isolated new collection;
4. retrieve that batch, compare exact IDs and payloads and vector directions,
   and calculate the new ref checksums; and
5. after all batches, require the provider's exact total count before normal
   activation.

This removes the current all-points list and the validation-wide expected and
actual dictionaries. The records snapshot may initially remain materialized,
but vectors and Qdrant objects must be bounded by batch. Insert staging refs
from an iterator or bounded chunks rather than another 100,000-row Python list.
If the required 100k run shows that the retained record contents exceed the
memory gate, paging those contents under a verified snapshot is a separate
follow-up; it is not necessary to weaken vector validation now.

An unchanged active-generation check should verify refs, exact provider
payloads/count, and checksums in batches. It no longer needs to call the encoder
for attested local generations. A legacy or imported active generation without
attestations may continue serving under its existing contract, but the next
explicit rebuild must locally encode its records rather than manufacture trust
from its numeric vectors.

Concurrent writes retain current behavior. The build uses its captured records
and event root, a newer write updates/coalesces the durable job, and activation
rejects the superseded snapshot. Cache hits are never inferred from the latest
record table after capture. A missing or corrupt old point safely becomes a
local miss; an unavailable old collection causes a retryable build failure or
an explicit full-rebuild recovery mode, without changing canonical data.

## Portability, recovery, and storage

Portable vector export remains unchanged. It exports the active vectors and
their existing manifest contract, not the local reuse attestations. Import must
write null checksum fields. The current import checks establish transport,
payload, cardinality, and internal vector consistency, but they do not prove
that the supplied values came from this installation's encoder artifacts.
Imported vectors therefore cannot seed reuse. A later local rebuild can encode
and attest them normally.

Keeping only the checksum on `dense_projection_refs` adds a few megabytes per
retained 100k generation instead of roughly 102 MB of raw 256-dimensional
float32 blobs per cache copy. It also avoids changing portable formats and
limits the effect on whole-SQLite backups. If the old provider collection is
lost, the checksums alone are not a backup and the safe recovery is a full local
rebuild. If a checksum is corrupt, only that record is re-encoded. If the entire
database is restored with its matching provider generation, attestations remain
usable; a provider mismatch produces misses or fails validation and cannot
activate corrupted state.

The current implementation retains older ready manifests, refs, and provider
collections. At 100k, repeated writes therefore have an existing unbounded
storage cost, and adding a checksum to each ref increases it. Do not add eager
post-activation deletion in this repair: a portable export can hold a SQLite
snapshot of the formerly active generation while it is still reading that
generation's Qdrant collection. Safe garbage collection needs a durable
generation read lease (portable export being the first holder), provider-first
deletion, absence verification, and only then deletion of inactive refs and
manifests. Until that lease-aware retention work and a repeated-write storage
soak pass, storage scale remains open even if the five-minute visibility run
passes.

A full vector-blob cache is not recommended for the first repair. It duplicates
Qdrant in the canonical SQLite backup, expands migration and quota behavior,
and still needs the same artifact fingerprint and provenance checks. It can be
reconsidered only if the authenticated provider reuse benchmark cannot meet the
five-minute limit because of reads from the prior collection.

## Implementation ownership

The implementation should be one coordinated change across:

- `daem0nmcp/migrations/schema.py` and `daem0nmcp/schema_version.py`: additive
  migration 31 and schema verification;
- `daem0nmcp/retrieval/dense_projection.py`: batched reuse/build/validation,
  checksum persistence, activation requirements, and recovery behavior;
- `daem0nmcp/retrieval/runtime.py` and
  `daem0nmcp/retrieval/onnx_encoder.py`: bounded batch inference and immutable
  artifact identity;
- `daem0nmcp/retrieval/providers.py` and
  `daem0nmcp/retrieval/vector_validation.py`: stable vector-space contract and
  canonical provider-vector checksum helpers;
- `daem0nmcp/api/v7/portable_projections.py`: explicit null attestations on
  import and unchanged external format; and
- focused schema, runtime, dense projection, portability, and real-provider
  tests.

The schema change should land only after migration 30 is stable. Do not overload
`build_config_hash` or the existing builder contract for reuse: both include
generation-specific collection configuration and therefore cannot identify a
stable vector space across generations.

## Acceptance tests and measurements

Meaningful deterministic tests must prove:

- an unchanged attested generation validates with zero encoder calls;
- changing one record re-encodes only that record while publishing a complete
  new generation;
- content/source-event, model, prefix, dimension, backend, artifact, payload,
  point-ID, checksum, and vector substitutions cannot reuse a point;
- a finite, correctly sized malicious vector is rejected unless it matches a
  prior locally anchored checksum;
- imported and pre-migration refs do not seed reuse;
- corrupt or missing prior points re-encode only affected records, while a
  provider transport failure remains retryable and cannot poison refs;
- output ordering, invalid batch output, duplicate/missing/unexpected points,
  exact cardinality, and both inference and Qdrant batch bounds are enforced;
- a source write during staging cannot activate the stale generation, leaves
  the prior generation active, cleans staging, and causes the durable latest
  source to be rebuilt; and
- migration 30 to 31, database backup/restore, portable export/import, and
  cleanup of historical generations preserve their existing contracts; and
- concurrent portable export plus activation never deletes its source
  collection, while a repeated-write soak reports and bounds SQLite and Qdrant
  growth.

The release gate then needs an actual configured-model/authenticated-Qdrant
100,000-record cold build that reports total duration, model-load, inference,
prior-provider read, upload, validation, activation, batch counts, and peak RSS.
No cold-build duration is inferred from the 257-record run. After a process
restart, make one canonical record change and measure from commit through an
active dense manifest covering that event. The post-write run must complete in
at most 300 seconds, encode exactly the changed record, retain the old active
generation until publication, and pass recall from the new generation. Repeat
enough times to expose warm variance and report every result. Until those runs
pass on the supported configuration, P3 dense scale remains open.
