# Independent C45 Windows OS identity phase controls

The original run40819426925 Server2022 cu118 clean scenario timed out in the whole
60-second verification-only PowerShell child. No per-statement stage was recorded.
This two-job diagnostic does not infer which statement stalled and never overrides
that original failure. It does not download a candidate ZIP, restore, launch GUI,
compile, install dependencies, enumerate processes, alter security or publish.

Each fresh standard Server2022/2025 job reads the exact C45 windows_verification.py
LF bytes and checks its literal original command and timeout by AST. It runs once
unchanged original control, then once with stderr-only stage markers. Original
operations and JSON remain, and both retain the original60-second communicate
budget. Process creation can add wall time. On timeout only the probe's own child
is cleaned up, raw partial stdout/stderr retained; no system process is queried
or terminated. The original and marked controls are separate processes in order;
this is not a reproduction of the old host, bundle interpreter or instantaneous
clean/contaminated environment. No causal claim or cold-WMI claim is made.

Known system PowerShell is verified against GetSystemDirectoryW. Numeric native
RtlGetVersion/sys.getwindowsversion, IsWow64Process2 and read-only HKLM64 metadata
are separately recorded after the two controls, never substituted for required
CIM identity. All ordinary identity assertions remain, failure stays failure.
The stage helpers derive from the already reviewed native/windows-restorer-os-
diagnostic/post_probe.py; no306 input or prior acceptance is reused.

The workflow is scoped to its new files on fix/windows-restorer. Existing seven
workflows and production branch remain untouched. Push triggers only this two-job
workflow; it does not dispatch the nine-job distribution acceptance or assert VC
redistribution qualification. Unit fixtures are explicitly mocked/offline and
never claim Windows measurements. Real native reports are the next evidence.
