# Dated check of the frozen v0.3.2 signatures on a Windows host

One page. Run it **once**, on **any Windows machine you are allowed to use** (a laptop, a VM), **on or after 2026-11-07T03:13:00Z**.
It is read-only: nothing is installed, no setting is changed, the installers are never started. A standard user is enough.

**Why that date.** The signing certificate (thumbprint `A5726E26325013EE238988B8688E07FAA77B532D`) has a 3-day life and expires at
`2026-11-07T03:12:51Z`. Both frozen installers carry an RFC 3161 timestamp from 2026-11-04T03:36:10Z / 03:36:11Z, so they should stay valid after
expiry. Offline tools (signify, osslsigncode, OpenSSL) agree; what has never been observed is **what a real Windows says** after the date.

## 1. What you need

| Item | Value |
|---|---|
| Windows | Windows 10/11 or Server 2019+, PowerShell 5.1 (in the box) or 7. Record nothing by hand: the script prints host, OS build, user, UTC time |
| Network | outbound HTTPS/HTTP to Microsoft's certificate services (`www.microsoft.com/pkiops`, `oneocsp.microsoft.com`). Offline hosts give an INCONCLUSIVE result (`-NoUrlFetch`) |
| Files (4) | `Agentic.Apps_0.1.0_x64_en-US.msi`, `Agentic.Apps_0.1.0_x64-setup.exe`, `SHA256SUMS`, `Verify-FrozenSignatures.ps1` |
| Frozen hashes | msi `9e5e6262f7830255333060193cc4c285fcf60a72ee70905f22792c12e12d58e5`; exe `5931b1914e732c7b8106fc19f26e63e806fd1b0f3b876b2236cd2e24410fde23` |
| Script hash | `Verify-FrozenSignatures.ps1` `0e143010a62837bd67c27624fc115096840cd00d9637f841c3ecdff34af3384c` |

## 2. Bring the files over (on the Mac that holds the copies)

The wrapper checks the local copies against the frozen hashes first and refuses on any mismatch. `ART_DIR` = a directory with **copies** of the two
frozen installers and `SHA256SUMS` (never the frozen directory itself; the evaluator keeps one at `windows-dated/artifacts/`).

```
ART_DIR=<dir with the copies> bash packaging/acceptance/windows-r0prime/tools/stage-for-windows-host.sh                    # dry run: hashes + instructions
ART_DIR=<dir with the copies> bash packaging/acceptance/windows-r0prime/tools/stage-for-windows-host.sh --copy USER@HOST --dest 'C:/Users/USER/frozen-check'
```

`--copy` is plain `scp` with your own ssh configuration (the host needs the Windows OpenSSH server; the destination folder must exist).
No SSH? Copy the four files by USB stick, SMB share or RDP drive redirection into **one** folder.

## 3. Run it (PowerShell on the Windows host, in that folder)

```powershell
(Get-Date).ToUniversalTime().ToString('o')          # must be 2026-11-07T03:13:00Z or later
Get-FileHash .\Agentic.Apps_0.1.0_x64_en-US.msi, .\Agentic.Apps_0.1.0_x64-setup.exe -Algorithm SHA256   # must equal the frozen hashes above
Get-FileHash .\Verify-FrozenSignatures.ps1 -Algorithm SHA256                                              # must equal the script hash above
powershell -NoProfile -ExecutionPolicy Bypass -File .\Verify-FrozenSignatures.ps1 -MsiPath .\Agentic.Apps_0.1.0_x64_en-US.msi -ExePath .\Agentic.Apps_0.1.0_x64-setup.exe
```

`-ExecutionPolicy Bypass` applies to that one process only. The script copies both files to a temp folder, hashes them, runs `Get-AuthenticodeSignature`, runs
`signtool verify /pa /v /all` **only if it is already on the host**, exports the signer and timestamp certificates and runs `certutil -verify -urlfetch`
on them, and prints a verdict table. It ends with the **output directory** and the **SHA-256 of `transcript.txt`**. Exit code: 0 = PASS, 1 = FAIL, 3 = INCONCLUSIVE.
Do not re-run it to "get a better result": if you must run twice, keep both output directories.

## 4. What to send back

1. The whole output directory (`%TEMP%\frozen-sig-verify-<UTC stamp>`): `transcript.txt`, `transcript.sha256.txt`, `*.certutil.txt`, `*.cer`. The `copies\` subfolder
   (the two 70 MB installers) can be left out. `Collect-Results.ps1 -OutDir <that directory>` zips it and prints the zip's SHA-256 (it skips `copies\`).
2. **Two SHA-256 values in the message text itself** (not only inside the zip): the transcript's and the zip's. They are how the evaluator knows the transcript was not edited.
3. One sentence: which machine (laptop / VM, Windows edition) and anything unusual (offline, clock changed, proxy, antivirus prompt).

Do not edit, "clean up" or re-save the transcript. Delete the temp folder afterwards if you like.

## 5. How the evaluator reads it

| Check | PASS looks like |
|---|---|
| Date | `now (UTC)` is at or after 2026-11-07T03:13:00Z and `leaf expired now: True`; the host clock is sane (`w32tm` line present) |
| Files | both `baseline-match=True`; the script hash equals the one above |
| Verifier | `Get-AuthenticodeSignature` `Status : Valid` for **both** files, thumbprint match `True`, a timestamper certificate present |
| Chain / revocation | `certutil` on the **timestamp** certificate exits 0; on the **leaf** a non-zero exit with CERT_E_EXPIRED (0x800b0101) is EXPECTED after the date and is not a failure |
| signtool | optional: if present, exit 0 and "Successfully verified" for both |
| Integrity | the transcript's SHA-256 recomputed by the evaluator equals the one in the message; verdict table says PASS for both rows |

Reading the verdicts:

* **PASS** (both files): this Windows host honours the timestamp after the leaf expired: the frozen installers still verify there. One host, one OS build: it is not a promise for every Windows version, and it says nothing about SmartScreen reputation.
* **INCONCLUSIVE**: run before the date, offline, or revocation not checkable. It does not count as the dated check: run again.
* **FAIL with `NotTimeValid` / an "expired" message after the date**: that Windows build did not honour the timestamp: the frozen installers are not usable there after 2026-11-07. Escalate; do not ship them as-is.
* **FAIL with `HashMismatch` / `NotSigned`**: the copy is not the frozen file (transfer damage): redo the copy, compare hashes.
* **FAIL with `NotTrusted`**: the host could not build the chain (root not fetched offline, or a proxy): re-run online and send both transcripts.

Limits to keep in the record: Authenticode behaviour after expiry on Windows is INFERRED until this run exists; revocation cannot prove a past state; the script itself
(`Verify-FrozenSignatures.ps1`) has been checked only by a static lint, never executed on Windows before this run: a script error is a script defect, not a result.
