# Native Windows byte-volume restorer — unvalidated candidate

This directory is an **independent candidate**, not wired into the release
workflow or its seven-package guard. No version, tag, old release asset or
required DLL/model/language is changed. It is not ready for distribution until
the native Server gates below pass on the exact source and accepted archive.

The two CUDA ZIPs exceed a single GitHub asset's size limit. Numbered volumes
are **transport of the identical original ZIP bytes, not compression or a smaller
application**. A generated `restore.exe` knows exactly one accepted ZIP and its
ordered volumes. Download that EXE and every matching `.zip.partNNN` into one
ordinary local directory; double-clicking restores the original top-level
package directory there. It does not launch the extracted application.

The candidate needs no customer Python, PowerShell, VC installer, CUDA Toolkit,
archive program or downloaded dependency. `/MT` statically links the C/C++ runtime.
The explicitly authorized **Server-diagnostic-only** revision uses pinned official
zlib 1.3.2's restricted raw-inflate source closure, not system tar. It is **not**
Windows 10/11, real-package restoration or real-GPU certification. Prior positive
restoration gates failed; neither this source change nor offline tests waive them.

## Fixed identity and data flow

1. Build-side `scripts/release_volume_transport.py` verifies the original ZIP,
   its exact `.sha256` and successful `.acceptance.json`; splits raw bytes into
   sub-2-GiB assets; and emits a strict transport manifest. Failed acceptance
   cannot be relabeled by this restorer. The transport is not a new archive.
2. `generate_metadata.py` calls `verify_transport(..., include_entries=True)`.
   It generates a C++ header containing immutable volume names/sizes/SHA256,
   whole-ZIP size/SHA256, original identity, and every member path/type/size/SHA256
   (including `bundle-manifest.json`). `fixed_index.py` additionally rehashes the
   complete joined original ZIP before/after bounded header inspection. It checks
   ZIP32/64 local/central headers, descriptors, offsets, gaps/overlaps and exact
   inventory agreement, then embeds each file's fixed data offset, compressed
   size and STORE/DEFLATE method. Unsupported formats fail instead of guessing.
   Implicit directories are added. Filenames
   become UTF-16 **integer arrays**, never raw JSON interpolated into source.
   There is no production fixture/skip-validation option.
3. The customer EXE never parses a supplied JSON/checksum to decide what to
   trust. Win32 reads/writes stream through a 1-MiB buffer; CNG/BCrypt computes
   each part and combined SHA256. The reconstructed file is flushed and read
   back. After reopening it with a retained read-only/no-write/no-delete handle,
   its full hash is checked again before extraction. Offsets and sizes are
   unsigned 64-bit; no entire part or ZIP is allocated in RAM.
4. Only that retained verified ZIP handle supplies immutable compiled ranges.
   STORE copies exact bounded bytes; DEFLATE uses `inflateInit2(-15)` with fixed
   input/output limits, 1-MiB buffers and bounded allocation. Truncation, invalid
   blocks, trailing input, wrong output size or SHA fail closed. No runtime ZIP
   parser, filename decoder, archive library, child process or current-directory
   change exists. Win32 wide APIs exclusively create compiled paths and pin each
   directory before its descendants. Every file is SHA-checked while writing,
   flushed, and read/hash-checked again from its output handle.
5. Every actual extracted entry is checked against the fixed table. Extra,
   missing, wrong-type, reparse, hard-linked, wrong-size or wrong-hash outputs
   fail. Only then does a same-volume `FileRenameInfo` with `ReplaceIfExists=FALSE`
   publish the complete root relative to the already held parent handle. An
   existing directory—even an empty one—is never replaced.

The manifest build directory intentionally permits only its defined parts and
sidecars. The native EXE is a **separate** output/release asset. Do not place it in
that directory before running the strict build-side transport verifier. Customers
may put the EXE next to downloaded parts: the EXE uses its compiled fixed names,
not the build directory's mutable manifest.

## Paths, permissions and interruption

* All source/destination ancestors are opened individually with
  `OPEN_REPARSE_POINT | BACKUP_SEMANTICS` and retained without delete-sharing.
  Directory read/write sharing allows ordinary creation/extraction; no delete
  sharing prevents renaming/replacing the held ancestors. File inputs retain
  only read sharing, blocking concurrent writes/deletes throughout validation.
* UNC/network/removable/substituted drives, user-supplied device namespaces,
  reparse points (symlinks/junctions), traversal, ADS/colon, reserved DOS names,
  ambiguous trailing spaces/dots and repeated separators are rejected.
  The local filesystem must support persistent ACLs. The archive validator also
  rejects filename collisions/aliases and short-name (`~`) archive components.
* Legitimate **user-path 8.3 aliases** such as `RUNNER~1` are canonicalized by held
  handles and allowed. They are not confused with an archive member alias.
* Internal I/O uses a controlled extended-length path prefix **only after**
  validating a normal drive-absolute path; arbitrary user device syntax is not
  accepted. An OS-reported extended module path from `GetModuleFileNameW` is
  converted to an ordinary drive path before the same checks; this does not
  permit user-supplied device paths. Actual >260-character EXE placement still
  requires native coverage. Individual components are bounded to 255 UTF-16
  units and full paths to 32,700. No registry or `LongPathsEnabled` policy is
  changed. Real CUDA inventories have members at least 191 characters long
  before the parent/staging paths, so MAX_PATH-only support is insufficient.
  The new extractor does not create a process or set a cwd, but actual Unicode,
  >260-character member and >260-character destination restoration remain
  mandatory Server gates. The separate observed atomic-rename error 206 is
  **unresolved**; it is not attributed to tar or fixed by changing extraction.
* A random `.vse-incomplete-...` directory is atomically created with an ACL for
  the current user and SYSTEM. No existing tree is reused. Preflight requires
  free space for merged ZIP + extracted bytes + 64 KiB per inventory entry +
  64 MiB reserve, **in addition to already downloaded parts**. This conservative
  check is not a reservation: write/extraction failures still fail closed.
* Cancel/failure leaves only the private, explicitly incomplete directory for
  inspection/manual removal; it does not recursively delete anything or create
  the final success directory. A retry uses a new stage. After success, cleanup
  only deletes the temporary ZIP after matching its held file identity and
  attempts removal of the now-empty directories; downloaded parts are retained.
* No elevation, network APIs, registry edits, antivirus/execution-policy changes,
  `Bypass`, `Unblock`, encoded scripts or automatic application execution exist.

**Threat boundary:** ordinary user-mode code is not a sandbox against a malicious
process already running as the same user, an administrator or the kernel. The
private ACL protects the new stage against other users; fixed hashes, ancestor
locks and output checks handle corruption and path substitution. Directory
read/write sharing does not forbid an equally privileged process from trying to
change the contents/security of its own tree. Output file/directory locks are
closed just before the atomic directory rename (Windows may require this); the
root and parent identity locks remain. There is consequently no claim of an
absolute race-free boundary against same-principal hostile mutation. Do not
run this in an attacker-controlled local directory. No security setting is
weakened to work around a platform failure.

## Rebuild offline on Server CI

Use the repository's x64 MSVC build environment (Server 2022 **and** 2025), and
existing, verified Microsoft notice files from the repository's pinned VC source
cache. Build tools remain on CI, never in the customer's transport.

```text
python native/windows-restorer/build.py --manifest D:\transport\NAME.zip.transport.json --output D:\restorer-build --notices-directory D:\verified-vc-source --acknowledge-redistribution-license
```

`--output` must be new. The notice flag is the distributor's assertion of
applicable existing rights, **not** automated license acceptance. Both existing
pinned Microsoft notices, existing Apache LICENSE and upstream zlib LICENSE are copied unchanged;
see `THIRD_PARTY_NOTICES.txt`. No dependency or license download is performed.
The compiler uses `/MT /O1 /Os /GS /guard:cf /W4 /WX /bigobj`; the linker embeds
`asInvoker`, ASLR, DEP, x64 and `/DEPENDENTLOADFLAG:0x800` (System32-only
static-import search) settings; `/LOADCONFIG` verifies that import-search flag. `dumpbin` checks imports contain only
allowed system DLLs, rejecting VCRUNTIME/MSVCP/UCRT/Python imports. Compiler log,
imports, PE headers, generated metadata and a hash-bound build record remain for
review. `zlib_build.py` verifies every upstream file against the pinned official
source hash, compiles only five C files with `Z_SOLO`/`NO_GZIP`, and records every
object's actual symbols and source dependencies. The final link map must contain
raw-inflate entry points and exclude gzip/deflate/crc/archive API symbols. A
successful build explicitly says **not client-certified**.

Only `OUTPUT/distribution/` is the reviewable distribution set: `restore.exe`,
`restorer-source.zip`, `build-record.json` and the original `notices/`. The outer
build directory's `.obj`, logs, intermediate files, SDK or Python environment
are **not** customer assets. Do not distribute the EXE alone: its exact source
and notices are required companions.

`source_bundle.py` provides tested `snapshot_sources`, `assert_sources_unchanged`,
`create_source_bundle` and `verify_extracted_sources` APIs. Its explicit allowlist
contains the exact C++ sources, generator/build/rebuild tools, their local
transport/guard/profile/bootstrap/runtime-dependency source closure, tests,
Apache LICENSE, exact restricted zlib sources/license/pin and notice pins. The source ZIP adds the **fixed generated
header**, original transport metadata, original build record and unchanged
Microsoft notices. Every member has a size/SHA256 manifest. Missing, changed,
extra or symlinked source files fail before rebuilding; no `.obj`, DLL, compiler,
SDK source or Python runtime is included.

The build record preserves both Git HEAD and full porcelain dirty/untracked
state (before and after the build), exact source hashes, `/Bv` output, hashes of
the actual `cl.exe`/`link.exe`/`dumpbin.exe`, actual consumed headers from MSVC
`/sourceDependencies`, and resolved library hashes from `/VERBOSE:LIB`. Windows
SDK paths/hashes therefore come from compilation, not merely environment labels.
Only child-process diagnostic language is set to English for reliable parsing;
no machine/user language or security setting changes.
Any third-party modules actually loaded by header generation are recorded; no
such Python environment is bundled. The fixed-header rebuild needs Python
stdlib only, even if metadata regeneration's existing guard import chain has
additional build dependencies.

Extract the source companion on an appropriately licensed native build host,
and run its own script with a new output directory outside the extracted tree:

```text
python native/windows-restorer/rebuild.py --source-root . --output D:\restorer-rebuilt --acknowledge-redistribution-license
```

This verifies the complete source manifest and compiles the included fixed
header without requiring the original multi-GiB ZIP, network or transport
Python dependencies. Re-generating that header instead requires the original
volume/receipt assets and the recorded generator dependencies. **Rebuildable is
not bit-identical.** The original record does not claim reproducibility; a
rebuild compares actual EXE SHA256 and toolchain binary hashes and records the
results explicitly. Neither a matching binary nor a source companion substitutes
for native restoration/package acceptance.

For a different local destination (must already exist):

```text
restore.exe --source "D:\Downloads\CUDA volumes" --destination-parent "D:\Applications" --non-interactive
```

`--non-interactive` suppresses only a completion/error message box, not a safety
gate. Neither form overwrites an existing restored package.

## Offline checks already available

```text
python -m unittest discover -s native/windows-restorer/tests -p "test_*.py" -v
g++ -std=c++17 -Wall -Wextra -Werror -pedantic native/windows-restorer/tests/core_test.cpp -o /tmp/vse-restorer-core-test
/tmp/vse-restorer-core-test
```

These cover metadata injection/UTF-16, strict validator integration and changed
parts, 64-bit sizes, path rejection, correct argv quoting, space arithmetic and
static-import filtering. A portable compiler test actually links the five C
files and tests valid/empty/multibuffer streams, truncated/invalid/trailing input,
size bounds and cancellation; source/index/link-map drift tests fail closed.
**Linux success is not Windows execution evidence.**

## Required independent Server 2022 + 2025 gate

The existing release workflow must remain unchanged while this candidate is
reviewed. A later separately authorized diagnostic job can run:

```text
python native/windows-restorer/tests/windows_server_tests.py --work-directory D:\vse-restorer-tests --large
```

Use a fresh local NTFS work directory with approximately 20 GiB free; preserve
logs, `fixture-build-record.json`, exact `fixture-source/` and
`native-restorer-report.json` as **diagnostic artifacts**, never release assets.
Do not upload the synthetic EXE/object as customer binaries. Fixture binaries and inputs are marked `SYNTHETIC` and carry no
package acceptance receipt. The script compiles `/MT`, checks PE imports and
runs Unicode/space/`&!%()` paths, poisoned PATH/SystemRoot/TAR_OPTIONS, a >260
character ZIP member and a >260-character Unicode destination cwd,
missing/modified/swapped/hardlinked volumes, ancestor
junctions, UNC/device syntax, existing-user-data protection, insufficient-space
preflight, a real >2-GiB stored ZIP/ZIP64 member, >2-GiB expanded DEFLATE,
invalid raw DEFLATE, wrong expected payload SHA, and abrupt interruption during
both merging and observed fixed-inflate output. The legacy first-case name still
mentions `clean-system-tar` only for continuity with prior recorded failures;
this candidate's restoration path never invokes tar. Without
`--large` the report is only partial; it must not satisfy the release gate.

Before the restorer cases, `system-tar-probe-report.json` records the actual
`GetSystemDirectoryW` tar path, SHA256, bytes, `--version` output, ACP/OEM/console
code pages, and before/after tar identity. Four direct-argv controls compare
ASCII/Unicode paths with ordinary/extended prefixes. Four stdin/cwd controls use
those same path classes, followed by ordinary/extended truly long Unicode cwd
controls. Each independently generated control archive has identical fixed
bytes and a Unicode member; reports check the exact Unicode output and payload
SHA rather than trusting console rendering or tar exit alone. All stdout/stderr
bytes are retained. Control failures are observations, **not a substitute for or
waiver of** the mandatory native-restorer gates.
After the common small fixture builds, each native gate records its own outcome
and continues to the independent gates, including ZIP64, after an earlier
runtime failure. A post-extraction SHA mismatch changes that case to failed;
one failed/blocked gate keeps the overall result failed and the test process
nonzero. Negative tests verify the shared source parts are pristine before and
after temporary mutations, preventing one failed cleanup from manufacturing
later apparent passes. Compilation/prerequisite failures remain explicit.

### Preserved actual failures and independent rename probe

Run `40268712355` at `810d07d` failed opening the Unicode archive; ZIP64 was not
reached. Evidence: 291,288 bytes, SHA256
`1789ff849364ca5d8332aa69be2014293bff018835b2d45b928d6b37af3f5f3f`.
Run `40271075684` at `29837c7` collected all 14 cases on both Servers. Ten negative
cases passed; **all four positive restoration cases failed**. Its evidence ZIP
is 1,155,631 bytes, SHA256
`efa72f8fb431181a87d15949fe12c6963c7ceee07d58bc43ef5899e4687bf68d`:

* Unicode member and real >2-GiB ZIP64: tar 3.8.9 reported unreadable member names,
  including the ASCII external-path control. ACP was 1252, console 65001; those
  are distinct. A seekable stdin handle did not solve member-name conversion.
* Deep >260-character member: extraction and SHA verification completed, but
  atomic `FileRenameInfo` publication failed with error **206**.
* >260-character Unicode cwd: `CreateProcessW` failed with error **267**, matching
  the documented process-cwd limitation. It did not reach tar extraction.

These are separate observed failures. Neither an upstream issue nor a lossy
console string alone establishes a unique cause. Historical tar controls are
retained, not retried as a production solution. No locale, security policy or
ASCII payload/path workaround is used.

The synthetic rename probe runs independently in the same selected MSVC host:

```text
python native/windows-restorer/tests/run_rename_probe.py --work-directory D:\fresh-rename-diagnostic
```

It collects 48 independent cases: old exact-sized relative-name buffer, explicit
NUL-terminated relative name, and NUL-terminated extended absolute name with
NULL RootDirectory; crossed with ASCII/Unicode, short/deep child, short/long
parent and absent/existing target. Each process records API structure offsets,
file identities, marker contents, error and unchanged existing-target sentinel.
No case failure/crash hides later cases; any unmet expectation keeps the total
nonzero. The **current Microsoft Win32 contract permits a relative RootDirectory**;
no NT-only assumption is used. The old production rename stays unchanged until
Server evidence isolates the failure and a reviewed fix passes no-overwrite gates.

### Restricted zlib provenance and known-risk boundary

Only unchanged official zlib 1.3.2 files are included, release commit
`da607da739fa6047df13e66a2af6b8bec7c2a498`. The official tar is 1,502,830 bytes,
SHA256 `bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16`.
[Official release](https://zlib.net/) and
[tag](https://github.com/madler/zlib/releases/tag/v1.3.2) identify the source;
`third_party/zlib-1.3.2/pin.json` binds every selected file. GPG verification was
not performed and is not claimed. License/source notices accompany all builds.

`inflate.c`, `inftrees.c`, `inffast.c`, `adler32.c`, `zutil.c` plus their seven
headers are compiled with `Z_SOLO` and `NO_GZIP`; no gzip writer, compressor,
CRC implementation or general archive parser is present.
[Known issue 1310 / CVE-2026-85091](https://github.com/madler/zlib/issues/1310)
affects `gzwrite.c`; the
[maintainer fix](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148)
changes that excluded file. Source and symbol-closure checks must demonstrate
exclusion on the **actual MSVC binary**. This is not a claim that the complete
1.3.2 release is vulnerability-free or that raw inflate has no unknown risks.
Use is currently authorized only for Server diagnostics, not customer release.

Read-only inspection of both accepted original CUDA ZIPs was actually run on
Mac with the frozen inspector (SHA256
`5497ced512459ad11006504162df27f0f143a3b83abf212ae6b79544a287b113`).
The returned evidence ZIP is 4,822,594 bytes, SHA256
`fb9e35aed7dfad356aee1050beb2437c45482ea229515f78344154a209c91e3c`:

| Original ZIP | Original bytes / SHA256 | Entries / offset-only ZIP64 |
| --- | --- | --- |
| CUDA 11.8 | 2,985,596,865 / `c13df10802ae5839c77c67bdab8b17f4b24a8e04ae3b11b90b3fd119681e97db` | 17,142 / 7,306 |
| CUDA 12.6 | 2,696,583,328 / `0e38570aa000082c9824c2fb51ba317ce72e8f0463619c08d28398a4faf177ac` | 17,113 / 6,975 |

Every member uses DEFLATE with flags 0, no local ZIP64 size or data descriptor,
and no unsupported compression feature. Both whole-file SHA checks before/after
inspection matched the original accepted hashes. CPython legitimately promoted
central `version_needed` 20→45 when only the offset needed ZIP64; the narrow
exception verifies precisely that structure, not arbitrary header disagreement.
This proves observed **layout compatibility only**: no payload was decompressed,
no native restorer ran, and no GPU/client certification is inferred.

The separate gate must ALSO establish these integration facts, not merely
summarize the synthetic report:

1. Build against each **successful real CUDA ZIP** and original receipt at the
   exact intended commit; record original ZIP SHA, volume SHA, embedded-header
   SHA, restorer SHA, zlib source/object/link-map evidence and MSVC version. Failed/stale jobs
   are ineligible, irrespective of a synthetic fixture pass.
2. Restore both real 11.8/12.6 packages in Unicode/special-character parent
   directories on each Server. Independently compare every restored file with
   the accepted inventory and rerun the existing package acceptance harness on
   those exact outputs. The completed hash-bound reports remain attached; CPU
   acceptance is never promoted to real-GPU certification.
3. Exercise a legitimate 8.3 path from `GetShortPathNameW` when the test volume
   provides one. Record an explicit unavailable result if the volume has 8.3
   creation disabled; do not change volume policy to manufacture a pass.
4. Inject a junction at an output ancestor and, in a controlled private fixture,
   an internal reparse/extra file before verification; prove rejection and an
   external sentinel unchanged. Check held ancestor rename and part write/delete
   attempts fail during restoration. Test Ctrl+C and forced death **during extraction**,
   not only during initial merging, and verify no success tree.
5. Test a genuinely capacity-limited destination (a pre-provisioned CI test disk,
   not a modified customer machine) to exercise write-time disk exhaustion after
   preflight. The fixed extractor needs no system tar and must never download a
   dependency. Any platform setup
   needing new authority stays a documented blocker rather than a bypass.
6. Record loaded-library provenance under contaminated environment and prove no
   customer VC/Python dependency. Neither Server has a real GPU acceptance claim.

If any gate is skipped, failed or unavailable, retain that fact and block release
of this transport. This directory does not schedule, publish or modify CI.

API contracts: [CreateFile sharing and reparse flags](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info),
[Microsoft argv parsing](https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments).
The historical [documented cwd length limitation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setcurrentdirectory)
is not confused with extended file-I/O support or the separate rename gate.
