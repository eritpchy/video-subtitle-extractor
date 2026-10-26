# Exact 8f restorer distribution experiment — not a release

This isolated driver is intended only for `eritpchy/video-subtitle-extractor`,
`refs/heads/fix/windows-restorer`. It adds one narrowly triggered workflow; the
seven existing workflows and all application/native-restorer sources remain
unchanged. Apply only on the independently checked diagnostic base
`6470357ce59b69fea51408cb6cfe1ab48a010619`.

## Fixed inputs and authority

`pins.json` binds the **2.3.0 build-only** application source
`8f08e7f6887a950f75cb7967f1f80e344935ecaa`, successful main run `40391756697`,
actual producing attempt **2**, and the two canonical CUDA artifacts. These are
not the previous 306 baseline and are not final 2.3.1 artifacts.

| Profile | Canonical artifact | Whole inner ZIP SHA-256 |
|---|---:|---|
| CUDA 11.8 | 12691536652 | `270ff11159d99bc4be236ad42867b9a5a59c10971af7ab1489b117d2e5accb38` |
| CUDA 12.6 | 12689727768 | `c1343fa5f998ac8504eb716d5933c6cef8428b544591cfbab15e15af30e237ab` |

The producer requires the exact API artifact ID/name/size/digest/source/run,
expiry, and actual successful attempt-2 producer job. It then hashes the entire
outer download. Its **exact three** members are the original ZIP, acceptance
receipt, and checksum. Each member is independently size/SHA checked before
unchanged `release_gate.py prepare` sees it. The separately preserved size.json
is not incorrectly treated as an outer member. Its expanded-byte values in
`pins.json` must agree with the fully verified transport.

Expiry is fixed to `2026-10-27T20:55:03Z`. Expired/missing/different artifacts
fail; there is no fallback to another build or synthetic passed receipt.

Only the two already-reviewed, fixed Microsoft license DOCX byte hashes in
exact8f are downloaded; no installer, toolchain, model, or library is added by
this driver. A changed notice stops the build. Push on this dedicated ref uses
the explicitly authorized existing redistribution acknowledgment. Manual runs
require an explicit boolean acknowledgment, default **false**. This is not a
new license acceptance or a permanent repository setting.

## Nine jobs and byte binding

1. Two separate Windows Server 2022 producer jobs, one per CUDA profile. Each
   invokes the actual production `prepare` **once**, yielding one EXE, byte
   volumes, fixed metadata, notices, and an exact-source ZIP. Matrix outputs
   cannot overwrite one another.
2. Four consumers: each profile's **same producer EXE SHA** on Server 2022 and
   Server 2025. They use the original native restorer, check every restored file
   SHA, and run the restored **Start-VSE.bat**, not an invented replacement or a
   separately extracted original ZIP. Unchanged exact8f acceptance checks both
   clean and contaminated environments, 168 model CPU cases and fast/accurate/
   auto CPU video workloads. Every original input and payload after-check stays
   enabled. No GPU inference claim is made.
3. Two independent Server 2025 source-only rebuild jobs. **No checkout step**;
   no application ZIP, parts, original EXE, producer objects, or repository is
   downloaded. The workflow embeds `source_only.py` verbatim as a Python literal
   block. Only the source ZIP and independently anchored context are supplied.
   The contained `rebuild.py` runs with **`-I -S -B`**, using installed Microsoft
   14.44 and the original full `/MT`, PE, raw-inflate object/final-link-map and
   source inventory gates. No packaging dependency or license fetch is needed
   for this path. Rebuilt EXE bytes are recorded, **never substituted** for the
   producer EXE used by consumers; bit identity is measured, not assumed.
4. One `always()` collector checks all eight actual upstream conclusions and
   independently anchored raw report SHAs. It **downloads and hashes both full
   transports again**, invokes unchanged `load_handoff` / `validate_build` /
   `validate_consumer`, and rechecks the actual rebuilt link-map with the
   authoritative unchanged zlib validator. A failure/skip/missing report is a
   failure, never a partial green summary.

Source-only reports deliberately have a **new distinct scope**. They bind the
source ZIP, manifest, actual used source closure, generator identity, header,
original EXE, and producer handoff. They include native Server 2025 identity,
actual toolchain records, rebuilt EXE hash, and byte-bound raw compiler/map/
command/rebuild logs. Before/after source proofs must match that context.
`inputs_after` is **forbidden**: a no-repository rebuild cannot claim the legacy
rebuild CLI's local full-transport proof. The collector independently performs
that transport proof; no old report schema is impersonated.

Source/report action downloads use exact unique names in the **same run**;
external producer/report SHA checks are mandatory. Immutable artifact IDs remain
job outputs and are API-checked by the collector against the expected names,
source commit, run and digest. The delivery download itself is by exact ID and
requires the complete API-anchored outer SHA. Download credentials are used only
for the fixed GitHub API; they are not forwarded to signed storage URLs or
compiler/application children. Signed URL queries and full environments are not
logged. Child environment records contain only necessary keys/hashes.

## Layout, storage and narrowly authorized cleanup

Producer output is outside its exact clean8f checkout. **No second copy or move
of parts** is made. The delivery upload allowlist contains only:

- `prepared/volumes/**`
- `prepared/build/distribution/**`
- `prepared/handoff.json`, `producer-context.json`, `delivery-index.json`

The index and independently supplied index SHA bind this exact transfer set.
Compiler objects, original candidate ZIP/outer, application checkout, and host
Python are not included. The source-only artifact reuses the existing source
ZIP file, not a second locally compiled binary.

Let `O` be the original canonical outer bytes, `Z` the inner ZIP bytes, `D` the
actual indexed delivery bytes, `A` the actual transport outer API size, `E` the
actual validated expansion, `N` the actual inventory count and `R = 4 GiB`.
Every check uses **current real free space**, after checkout where applicable:

| Phase | Additional free space required before phase |
|---|---|
| Producer, before original download | `O + 2 Z + R` |
| Consumer, before transport download | `max(A + D, D + Z + E + 65536 N) + R` |
| Consumer, after verified redundant outer removal | `Z + E + 65536 N + R` (also rechecked by original gate) |
| Collector, before both profiles | `D118 + D126 + max(A118, A126) + R` |
| Source-only rebuild | source extraction bound `256 MiB + R`, with downloaded ZIP already present |

The producer minimum is 13,252,268,051 bytes (12.342 GiB) for CUDA 11.8 and
12,385,226,672 bytes (11.535 GiB) for CUDA 12.6, **in addition to the already
occupied checkout**. Transfer/extraction headers, logs and build outputs consume
the reserve; actual delivery/API sizes are not guessed.

Only a **job-created redundant transport outer** named `transport-outer.zip`
may be removed. Before that single-path operation, the driver requires:

- a newly created owned work directory strictly below actual `RUNNER_TEMP`;
- matching run/attempt/job/driver-SHA ownership marker;
- plain non-reparse ancestors/files, no hardlinked outer/evidence;
- full outer hash and exact extracted-member identities/content;
- a persisted, fsynced API/hash/extraction evidence document.

The exact removal and preserved upstream artifact identity are audited. The
remote artifact and its download/hash evidence remain. **Original candidate
outer, parts, EXE, source ZIP, original reports, unrelated directories and
preinstalled software are never deleted.** No glob cleanup, cache clearing,
process enumeration or security-policy change is permitted. Insufficient free
space still fails. The operation is intentionally not represented as loss of
upstream download provenance.

## Local contracts and reproducibility limits

Run from a workspace containing the small exact8f review sources (or the actual
application-source checkout):

```sh
VSE_REVIEW_SOURCE=/path/to/exact8f PYTHONDONTWRITEBYTECODE=1 \
  python -B -m unittest discover -s diagnostics/windows-restorer-distribution/tests -v
```

All source-only imports are stdlib. `source_only.py` and JSON use local `-text`
attributes to avoid autocrlf changing pinned source bytes. The workflow Python
bootstrap itself inherits `PYTHONDONTWRITEBYTECODE=1` and immediately sets
`sys.dont_write_bytecode=True`; it is **not** described as an outer `-I` process.
The actual contained rebuild is `-I -S -B`. Product helpers that spawn Python
without `-B` inherit the suppression setting; exact8f is not modified.

Offline fixtures test the real unchanged gate validators plus safe extraction,
independent raw hashes, same-EXE/two-Server binding, source-only scope, strict
compiler/PE/link-map evidence, ownership/cleanup negatives, budgets and workflow
contracts. The POSIX symlink fixture is not run on Windows hosts without symlink
privilege; a separate reparse-attribute rejection test always runs. None of
these offline fixtures is actual Windows restoration evidence.

This patch alone proves neither native building nor same-EXE restoration nor
source-only MSVC rebuilding. Those remain pending the authorized actual Server
run. It does not publish assets, change versions/tags, certify Windows 10/11, or
claim GPU execution. **Any final 2.3.1 ZIP SHA change requires new original ZIP/
receipt/checksum anchors, regenerated parts/header/EXE/source ZIP and this actual
same-byte two-Server/rebuild acceptance again.** The EXE is a distribution
helper; the Python application's normal execution does not require compiling it.
