# Future accepted-ZIP transport integration (not yet run)

This directory is full public build/rebuild source for the **distribution helper**
`restore.exe`. The application remains its existing Python application: neither
running it from source nor running a restored package requires compiling this
helper. Customers need no Python installation, Visual Studio, VC installer,
PowerShell archive script, system tar, or network fetch to restore volumes.

There is no new workflow, trigger, publication, tag, version or release-policy
change here. In particular, no 306 artifact pins or OS-diagnostic workflow were
merged. The seven-package release matrix/guard remains authoritative. Original
2.3.0 assets are untouched. Future 2.3.1 inputs **do not yet exist in this
contract**: every commit/version/archive SHA comes from the actual later accepted
bytes, not an illustrative hash, a renamed old archive or a manufactured receipt.

## Separate producer, consumers and source rebuilder

The four explicit subcommands are build-host/Server-only tools; none run in the
customer EXE. Use existing licensed x64 MSVC/SDK tools, Python 3.12 and the existing
`packaging` build dependency. No dependency is fetched by these tools.

1. **`release_gate.py prepare`** accepts `--archive`, its independently obtained
   `--archive-sha256`, `--receipt-sha256`, `--checksum-sha256`,
   `--commit`, `--version`, `--profile`, `--runner`,
   optional `--notices` (defaults to committed project attribution), a fresh `--work`, and the distributor's explicit
   `--acknowledge-redistribution-license`. Keep `--work` outside the clean source
   checkout. The adjacent exact `.zip.sha256` and successful `.zip.acceptance.json`
   are mandatory. Obtain all three SHA anchors independently from the same
   authenticated successful producer artifact, not by blessing edited sidecars.
   The authoritative transport/receipt validator splits only the
   original ZIP bytes; `build.py` runs **once** using `/MT` and its unchanged
   production PE, import, zlib source/object/map and no-synthetic-seam gates.
   The producer creates `volumes/`, `build/distribution/` and `handoff.json`.
   Bind the handoff SHA independently to the producer CI artifact digest.
   Run this once **per final CUDA profile/ZIP**: each EXE embeds exactly one ZIP,
   so CUDA 11.8 and 12.6 have separate handoffs and each requires two consumers.
2. **`release_gate.py consume`** accepts that hash as `--handoff-sha256`, plus
   `--handoff`, `--distribution`, `--manifest` (inside the volume directory),
   `--application-source`, `--runner` and fresh `--work`. Run it on **both**
   Server 2022 and Server 2025, passing the **same distribution EXE bytes**.
   It never compiles or substitutes a per-OS EXE. Native restoration is the only
   application ZIP extraction path. Every file/type/directory is verified before
   and after the original `Start-VSE.bat` acceptance in clean and contaminated
   environments. Application fixtures/audit helpers must come from the exact
   clean application commit embedded in the accepted ZIP. The existing validators
   require all 15 models/168 CPU cases and three CPU video modes; no GPU claim is
   inferred. Whole parts/receipt/manifest/distribution are rechecked even on error.
3. **`release_gate.py rebuild-source`** takes the same handoff/distribution/
   manifest arguments, `--runner`, a fresh `--work`, and the license confirmation.
   It expands **only** the source companion, never the application ZIP, into a
   separate tree. The companion's own `rebuild.py` runs with `-I -S -B`, from
   outside either source checkout, using only its fixed generated header and
   included exact source. `packaging`, original ZIP and network are not needed
   by this child rebuild. The outer orchestration still verifies the original
   transport inputs. No producer object files, SDK, compiler or Python environment
   enter the companion. A rebuilt EXE is evidence, **not** a replacement for the
   selected distribution binary; if choosing it instead, both consumer gates must
   run again against those new bytes. Bit-identical output is measured, not assumed.
4. **`release_gate.py combine`** takes the same handoff/distribution/manifest and
   two `--consumer-report` paths with corresponding independent
   `--consumer-report-sha256` values, `--rebuild-report` with
   `--rebuild-report-sha256`, and new `--output`. These report hashes must come
   from verified original CI artifacts; merely hashing an untrusted summary does
   not establish acceptance. It rejects
   missing/duplicate OS reports, different ZIP/EXE/source identities, incomplete
   launcher/tree evidence, synthetic builds and unproven rebuild symbol closure.
   A passing report still grants **no release permission** and is not independent
   cryptographic, Windows 10/11, physical-disk-exhaustion or real-GPU attestation.

Run `python native/windows-restorer/release_gate.py SUBCOMMAND --help` for exact
syntax. There are deliberately no sample SHA strings accepted as default pins.
CI acquisition and artifact transfer must supply their own authenticated exact
artifact/digest binding; this tool does not download files or add tokens to logs.
Retain complete raw model/video/launcher evidence and build/source/map/PE records,
not only the summary JSON. Consumer reports bind the launcher tree inventory and
raw log hashes; combined reports preserve independent original-report SHA/size.
Producer, consumer and rebuild failure paths still run input after-checks and
retain primary plus secondary errors. No handoff is published after failed
producer post-checks. No new trigger is enabled by these entry points.

## Space and unchanged inputs

Let `Z` be ZIP bytes, `E` expanded file bytes and `N` file+directory inventory
entries. With the original ZIP already present, the producer requires at least
`Z + 4 GiB` additional free space for volumes, source header and compilation.
With downloaded volumes already present, each consumer requires
`Z + E + 64 KiB*N + 4 GiB`; the customer's native preflight remains its existing
smaller exact bound and is not replaced by this CI budget. The source-rebuild
stage bounds source expansion to 256 MiB and requires its bytes plus 4 GiB free.
Existing files are never overwritten and no cleanup of unrelated data occurs.
Large payloads, models and original archives are never copied into source ZIPs.

The accepted ZIP and both original sidecars are byte-preserved; consumers hash
every volume again. The output directory is Unicode with spaces and `&!()`;
the native synthetic suite separately covers `%`, long paths, ZIP64, invalid
streams and interruption. The existing batch launcher contract does not accept
arbitrary `%` paths; no replacement launcher is invented to hide that boundary.

## Source closure and rights

`source_bundle.py` preserves exact C++/zlib/generator/build/rebuild/gate/test
sources, fixed generated header, transport metadata, build provenance and all
four required attribution/third-party notices (not the old network DOCX documents). Actual header-generation imports are hashed and must be
within the snapshot. The current authoritative reachable script closure is
`release_volume_transport`, `release_guard`, `package_release`, `bundle_bootstrap`,
`runtime_dependencies`, `release_profiles`; unused lazy platform-packaging paths
are not copied merely because they occur in `package_release.py`.

The restored-application acceptance is intentionally a separate clean full source
checkout at the application's exact commit, not a claim that the helper's small
source ZIP contains the full application, fixtures and all platform build systems.
Publish the project's full source release too. Preserve Apache-2.0, unchanged zlib
LICENSE/source pins and the original Microsoft notice documents. Acknowledgment
does not create toolchain/runtime redistribution rights. Before actual release,
review those rights and current security status again; the historical restricted
zlib review is not a claim that the whole upstream release is vulnerability-free.

## Existing evidence and remaining limits

The imported native implementation and zlib/build closure match the diagnostic
source at `29f9ceefcf7930faec4634c3e8f2f5c4a15b25e0` (unchanged through
`6470357ce59b69fea51408cb6cfe1ab48a010619`). Run `40299168461` passed all 19
native synthetic cases on both Servers, including true >2 GiB ZIP64 and expanded
DEFLATE, Unicode/long paths, malformed streams and interruption. The disk-space
case simulated the Win32 available-space report; it did **not** exhaust a disk.
Independent rename evidence consists of 32 strict API controls and 16 safe
invalid-input observations, not 48 successful restorations.

Accepted application commit `3064320ec74e1aa71f3702c2ea966485f4b31e33` has actual
two-profile/two-Server restoration and original-launcher evidence from
`40308041109` (three successes, one preserved timeout failure) and
`40328495164` (later isolated Server-2025/CUDA-12.6 success). The intervening
`40318600800` failed its evidence-freeze gate and remains failed. Later success
does not explain or erase either earlier failure. These are **306 byte-specific**,
not future release evidence. Those jobs compiled different EXEs per OS; they do
not yet establish the new same-EXE distribution contract.

This integration's new orchestration, actual `build.py` distribution path,
source-ZIP independent rebuild and final-SHA rebinding have **offline tests only**
until separately authorized future Server jobs run. The native C++ security
boundaries and remaining gates in README are not waived. No version, security
policy, GPU proof or publication status is advanced by this source integration.


Build-only attribution revision: optional `--notices` supplies the pinned
project notice; default uses committed source, without external DOCX requests.
The complete companion contains that notice and its manifest. Applicable
Microsoft terms/notice/flow-down obligations remain unverified; technical
acceptance is not publication authority. No DLL, /MT, PE, zlib, inventory,
same-EXE or launcher gate is relaxed.
