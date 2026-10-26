# Original Paddle model seed (build input, not new inference evidence)

The standard macOS job for commit 59749cc reached full source/GUI acceptance,
then one original-model archive request returned HTTP 403. That old log did not
identify the model or URL. Do not infer which of the 15 models was denied, retry
that request, or replace its source with an unreviewed mirror.

A legitimate already-accepted CPU ZIP from the same source is available:

- Source run 40259119763, attempt 1, commit
  59749ccc02e369f374c69846b2bde6279084577d.
- Recorded artifact 12624088363:
  release-candidate-windows-x64-paddle-cpu.
- vse-2.3.0-windows-x64-cpu.zip: 583979746 bytes,
  SHA256 6318016c3e0e1452e2aa88d1ca1757b0934a02f677d749d69330a91836bc9fc8.
- Its original acceptance receipt SHA256:
  dbca5f75e163bba0620741c1cfee96a65b476279c22aeb7fbf0c4337b51c9f41.

This is a build-only candidate identity, not a replacement for the already
published 2.3.0 assets. The source run and hash stay distinct from a future
consumer checkout's commit.

## Fail-closed reuse

The build-side scripts/paddle_model_seed_ci.py binds those external hashes before
invoking the seed extractor. Only the 45 original model files, fixed source pins
and canonical model inventory enter models/; a separate canonical provenance
record identifies the source. No runtime files are extracted or executed.
The existing complete-model verifier independently checks all 15 models.
A local CI receipt is not a signed attestation.

The reusable paddle-models.yml restores an exact pins/archive-keyed cache,
or bootstraps from that exact run/name if absent. Both paths reverify all source
anchors, inventories and model hashes. An invalid cache fails rather than being
silently discarded. A same-run SHA-named artifact supplies all Paddle consumers;
each consumer verifies again and performs its existing source/final-package
CPU inference and clean/contaminated-launcher gates. The seed does not replace
any of those tests or claim GPU execution.

Neither a producer nor a consumer falls back to a model-origin download.
The standard source checks use VSE_MODEL_DIR pointing at the verified complete
tree instead of modifying tracked model files.

## Storage is not permanent

The recorded bootstrap artifact expires **2026-10-27 02:26:40 UTC**. A requested
artifact retention period may be limited by repository policy. Actions caches
are evictable, not a durable model repository. If both are unavailable, the job
must fail clearly; an old download failure is not authorization to retry.

The maintainer has preserved the original accepted CPU ZIP locally. Before the
ephemeral artifact expires, preserve the verified model-only seed and provenance
in the authorized durable handoff. Update this fixed bootstrap contract only
after reviewing that new source's exact hashes; after a future authorized
release, its immutable accepted CPU asset can become the durable bootstrap.
Do not publish a new seed asset, overwrite an old release asset, or change
original model pins merely to extend CI storage.
