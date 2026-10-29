# Exact 2720 CUDA same-byte restorer validation

Application target: `2720ab309cdbce3c63bc9a7e2748930f3c1cafec`, version **2.3.1**.
Original main run **40834467106 / attempt 1** completed with all 22 required jobs successful;
only three duplicate producers intentionally skipped. Candidate-set job **123330516490**
validated all seven new canonical artifacts. Standard CI **40834375090 / attempt 1** was 6/6 successful.
Driver patch base: `d9141a2fcd610d4706e7d03e42424a30bd1181b3` on the separate diagnostic branch.
No application source, existing published asset, tag, default branch or permission is changed.
This driver does not supply redistribution authorization or claim that its nine Server jobs have
passed. The actual diagnostic run, original reports and strict collector are the execution evidence.

## Immutable explicit binding; incomplete means non-executable

`pins.json` schema2 declares the repository, exact application SHA/version, successful main run,
actual attempt, actual candidate-set job, both canonical producer jobs, artifact IDs/names,
outer size/digest/expiry, original inner ZIP and checksum/receipt size/SHA, expansion and exact
committed project-notice identity. The checked-in pins now bind the actual successful 2720 attempt 1, canonical artifact
**12882245121** (CUDA 11.8) and **12881438248** (CUDA 12.6), expiring **2026-10-30 07:17:19 UTC**.
No old attempt 1, 8f, 306 or 2.3.0 bytes are substituted. Incomplete or changed future inputs
remain rejected before network, platform/toolchain activity or work-directory creation.

The exact JSON bytes are SHA256-anchored and base64-embedded in `source_only.py` and its two
verbatim workflow copies. The driver requires its checked-in JSON bytes to match that anchor;
source-only jobs need no checkout, extra input download or environment-based source override.
The producer context carries the entire selected application identity and binding hash, not
just a source commit. A changed source/version/run/attempt/artifact/profile or sidecar is rejected.
There is no attempt==2 assumption: the declared positive integer attempt must match the actual
main, producer and candidate-set APIs, all completed/successful on the exact release workflow.

After any independently reviewed, qualified input update, synchronize locally with:

```
python -B diagnostics/windows-restorer-distribution/sync_binding.py --write
python -B diagnostics/windows-restorer-distribution/sync_binding.py --check
```

The synchronizer only updates local anchors, seven source checkout refs and two literal
bootstraps. It never downloads, builds, dispatches or approves anything; runtime validation of
complete inputs remains mandatory. The application commit and diagnostic driver commit/run
remain distinct: original canonical artifacts belong to the application main; generated
EXE/parts/source/report artifacts belong to this driver's own exact run/attempt.

## No restricted DOCX request and no implicit rights assertion

The old `acquire_notices` HTTP function is removed. No Microsoft DOCX or equivalent cached copy
is read. The driver validates only the **committed project attribution**, byte-for-byte against
its explicit pin and schema2 application manifest; the production 2720 gate uses its existing
default notice input. The source companion verifier preserves exactly four required project,
Apache and zlib notices. Project attribution is not a Microsoft license agreement. Applicable
terms remain `applicable-terms-not-independently-verified`, publication authority false.

The workflow is **manual only**, with `acknowledge_vc_redistribution_license` default false.
Push never means acknowledgment. The user explicitly authorized the existing VC redistribution
qualification assertion and this final Server validation. An authorized manual dispatch must
set that input explicitly; it accepts no new terms and does not authorize publication.
No persistent acknowledgment variable is created. A 403 propagates without retry/fallback.

## Preserved nine-job and binary boundaries

- Two Server2022 producers each call real production prepare once and retain original canonical
  inputs. Full authenticated outer digest, exact three original members and hashes are required.
- Four Server2022/2025 consumers use the same producer EXE per profile, never recompile it or
  substitute direct ZIP extraction. Original launcher, full before/after trees, both environments,
  model/CPU/video checks and every input after-check stay required. No GPU inference claim.
- Two Server2025 source-only jobs have **no checkout**, no original application ZIP/parts/EXE
  or producer objects. The included rebuild runs `-I -S -B` with installed MSVC14.44; exact
  source/header/notice/transport identity, `/MT`, PE/import, five raw-inflate objects and link-map
  checks remain. Rebuilt EXEs are recorded, never substituted for the selected producer EXE.
- One always collector rejects any missing/failed/skipped upstream or independent raw hash,
  rehashes full transports and binds the two original consumer reports and source-only result.
  Source-only scope forbids legacy `inputs_after` impersonation. Rebuildable is not bit-identical.

Artifact credentials remain confined to the exact GitHub API and are never forwarded to signed
storage URLs or child compilers/apps. Signed URL queries and full environments are not logged.
Real-free-space budgets, original narrow owned redundant-outer removal, no system cleanup,
all source/parts/EXE/report retention and original failure/secondary-error preservation remain.

## Reproducible local-only validation

Use a real canonical task-owned temporary directory on macOS: its default `/var` alias is
correctly rejected by unchanged plain-ancestor checks. Do not weaken the production check.

```
TMPDIR=/absolute/task-owned/real-temp VSE_REVIEW_SOURCE=/absolute/exact-2720/source \
  PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover \
  -s diagnostics/windows-restorer-distribution/tests -v
python -B diagnostics/windows-restorer-distribution/sync_binding.py --check
```

Fixtures are explicitly OFFLINE MOCK identities/records. Tests use real small Git repositories
and real source companion ZIP bytes, but do not compile or restore a native distribution,
contact GitHub/Microsoft, launch local GUI, enumerate processes or certify Server/physical GPU.
The original failed regression run and macOS temporary-alias run remain evidence.
A separate source/AST audit compares retained safety functions against exact5df. YAML and
embedded Python checks are local static review; actionlint is not installed by this patch.
Actual execution still requires actionlint, reviewed complete pins, explicit authorization,
and all nine native Server jobs before any distribution claim. No release is created.

## Review clarification: evidence identity and network boundary

Real-source tests obtain their expected commit/version from the reviewed checked-in pins,
never from the checkout under test. A clean arbitrary HEAD is explicitly rejected. A separate
fixed-2720 audit rehashes raw commit/tree/blob Git objects and checks all 55 delivered source
files against membership in tree `6b2a4591b4613eb45fdd641f55f5053217519cd1`, anchored by commit
`2720ab309cdbce3c63bc9a7e2748930f3c1cafec`; a self-consistent SHA256 inventory alone is not proof.
The import contract scans every `Import` alias and every `ImportFrom`, rejects relative or
non-stdlib imports, and includes negative fixtures for both previously omitted forms.

The offline test audit records only that local test process's network attempts. It is **not**
a zero-network workflow claim: even with acknowledgment false, the always collector can execute
checkout/setup actions before rejecting skipped producers. Such actions may use network. No
acknowledgment=false workflow was dispatched here. A successful source rebuild still does not
guarantee a bit-identical EXE; compiler/SDK/path/linker differences are recorded, never waived.


## Literal Git source-byte gate

The original `40819426925` diagnostic run compiled the exact clean C45 checkout,
but its source companions contained 39 normal Windows CRLF conversions among 55 files.
Those original reports and ZIPs remain evidence; normalized equality is not literal
Git-blob equality. The application candidate ZIPs and their pins are unchanged.

Both producer jobs now disable automatic checkout conversion through job-scoped Git
command environment only, before checkout. No global or repository Git settings are
written. A read-only preflight rehashes the selected commit, tree and blob objects,
requires all 55 checkout files to be literal authenticated Git bytes, and records an
audit outside the application checkout. The producer independently revalidates the
complete audit before network/workload. The reviewed command-scoped conversion setting
is explicitly propagated through the clean MSVC environment into the unchanged build's
own Git provenance checks; no arbitrary parent Git configuration is forwarded.
Producer and collector also compare every delivered sourceZIP member directly against
that authenticated Git tree. No source repair or newline-normalization fallback exists.

Real small Git fixtures reproduce a clean CRLF checkout that the new gate rejects,
and a job-scoped LF checkout that passes. Empty/duplicate audit lists, wrong tree/blob/
SHA, materially changed source, CRLF ZIP members, missing members, foreign HEAD and
corrupted raw objects fail. These offline contracts are not Windows compilation proof.
New native execution is required for the new diagnostic commit; original C45 application
CI and old diagnostic evidence remain separate. Rebuilt EXEs need not be bit-identical.


## Exact2720 identity and timeout evidence boundary

This binding update changes only pins, their synchronized bootstrap/checkout anchors, and this
README. All nine-job driver logic, literal Git/LF checks, original seven legacy workflows,
production build.py, fixed ZIP bytes and 60-second original OS-capture behavior remain unchanged.
The application commit2720 includes the reviewed Defender parent capture fix. Native identity
proposal dee8c1a is not injected into these packages or checkouts and is not certified here.

The separate d914 run40834517723 succeeded on both Servers. Its original PowerShell controls
took48.453s/34.781s and later marked controls23.516s/22.797s; this did not reproduce the original
60-second timeout or identify which old statement hung. Neither warm/cold state nor a causal
root-cause resolution is claimed. Any new timeout remains an actual failure; no retry, budget
increase, synthesized identity or fallback is introduced. Old40819426925 remains6success/3failure.

The nine jobs must consume this new exact2720 pin set. No C45 receipt, old producer EXE,
source-only reconstruction or diagnostic source overlay can stand in for a2720 delivered EXE.
GPU hardware and final local GUI acceptance remain outside these CPU-only Server jobs.
