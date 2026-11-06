# Snap Store packaging of Agentic Apps R0-prime (private draft, nothing submitted)

Purpose: say exactly what the Snap Store package is, what the store restricts, which parts of the app need which interface, and
which steps only the owner can take with the real Snap Store account. Nothing in this document or in `snap/` has been registered,
uploaded, reviewed or released. The product is a private candidate; passing a snap build or the store review tools is not a release
and not a statement that anything is cleared.

Status of the evidence: built and run in a disposable Ubuntu 24.04 LTS **aarch64** VM (Lima, Apple Virtualization) only. The
amd64 snap has not been built or run anywhere.

## 1. What the snap contains

`snap/snapcraft.yaml` (core24, `confinement: strict`, `grade: devel`):

* one part, `app`, that runs the same pinned pipeline as the `.deb` (`packaging/build-wheelhouse.sh`, `build-sidecar.sh`,
  `build-notices.sh`, `build-sidecar.sh` again with the Licenses folder, `stage-sidecar.sh`, `build-launcher.sh` with
  `LAUNCHER_BUNDLES=deb`) from `snap/local/build-payload.sh`, under an `env -i` allowlist, then unpacks that `.deb` with `dpkg-deb -x`.
  The snap therefore carries the same launcher binary, the same frozen sidecar and the same `Licenses/` folder as the `.deb`
  of the same build; there is no second recipe to keep in step. Toolchain versions come from `packaging/toolchain.env`
  (uv 0.12.16, Rust 1.98.1, tauri-cli 2.11.4, CPython 3.12.10) and are installed into the part's build directory, not the host.
* `bin/agentic-apps-snap` (from `snap/local/agentic-apps-snap`): sets `AGENTIC_OS_HOME` and `REDEVOPS_SECRET_DIR` to directories
  under `$SNAP_USER_COMMON` and runs the launcher. No permission is requested by it.
* `snap/gui/` desktop file and icon (the icon is the existing neutral temporary artwork).
* stage packages: `libwebkit2gtk-4.1-0`, `libayatana-appindicator3-1`, `xdg-utils`; GTK comes from the `gnome` extension
  (content snap `gnome-46-2404`).

## 2. Confinement and interfaces

| Interface | Why | Store handling |
|---|---|---|
| `network` | the control plane calls hosted-model and connector APIs the user configures; the launcher's legacy-listener probe uses loopback | auto-connect, no review |
| `network-bind` | the sidecar listens on `127.0.0.1` (the loopback-only local UI and API) | auto-connect, no review |
| `desktop`, `desktop-legacy`, `gsettings`, `opengl`, `wayland`, `x11` | added by the `gnome` extension for the GTK/WebKit launcher window | auto-connect, no review |

Deliberately **not** requested: `home`, `removable-media`, `docker`, `system-files`, `ssh-keys`, `password-manager-service`,
`process-control`, classic confinement. `snap connections` in the test VM is reproduced in section 6.

### What breaks under strict confinement, and the options (owner decisions)

The launcher has three optional buttons (Enable / Disable / Remove local containers) that run `docker compose` of the HOST
(`deploy/launcher/src-tauri/src/main.rs`: `advanced_docker_up/down/purge`). They run only on a user click and are not needed for the
native default. Inside a strict snap there is no `docker` binary and no docker socket, so they fail closed with
"failed to launch" (no silent fallback, nothing requests Docker access).

* **O-SNAP-1 (recommended default for a first store listing): strict, no docker interface.** The advanced container tier is not
  available from the snap; the native control plane works. Nothing to approve.
* **O-SNAP-2: strict plus the `docker` interface.** The snapd `docker` plug gives access to the host's Docker daemon socket, which is
  root-equivalent on the host. It is not auto-connected and requires a store super-privileged / manual-review grant with a
  justification; the user would also have to run `snap connect agentic-apps-r0-prime:docker`. The snap would still need a docker
  CLI inside it (it has none) and `docker compose` project files outside the snap. Do not apply without the owner's decision on
  whether the advanced tier justifies a root-equivalent interface.
* **O-SNAP-3: classic confinement.** Needs a store manual review and a written justification (the store accepts classic only for
  categories such as IDEs/compilers or tools that need arbitrary host access; a governed AI control plane is not one of the usual
  categories). A classic snap has the user's full privileges and no sandbox. It would also change what "the package touches" on
  Linux for the whole product. Not recommended; not applied.
* **O-SNAP-4: do not offer the advanced tier in the snap at all.** The three buttons can be hidden when running under snap
  (`SNAP` set). That is a launcher UI change on the frozen line and is not made here.

* **O-SNAP-5: the WebView problem (section 6, SNAP-D1).** If the investigation shows that WebKitGTK's process sandbox (bubblewrap,
  unavailable inside a strict snap) is the cause, the usual snap workaround is to turn that sandbox off for the launcher window
  (`WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` in the wrapper) and rely on snap confinement instead. That weakens a control of the
  launcher (the window loads only the static launcher page under a `default-src 'none'` CSP, and the sidecar origin is never loaded
  into it), so it is an owner decision and is **not applied**; `tests/test_snap_packaging.py` fails if it is added silently. The
  alternative is a `browser-support` plug with `allow-sandbox`, which is a wider, store-approved interface and also not applied.

### Other store-relevant behaviours of this app

* Opening the UI: the launcher writes a private redirect page (0600, in a 0700 directory) under the app cache directory and hands
  its **path** to `xdg-open`. In a snap, `xdg-open` goes through the desktop portal; whether the user's default browser (itself
  possibly a snap) can open a file from `$SNAP_USER_DATA` depends on the portal's document handling. Section 6 records what the test
  VM showed.
* The sidecar is a PyInstaller one-file executable: on every start it unpacks about 80 MB into `TMPDIR` (inside a snap, the
  snap-private `/tmp`) and removes it on a clean exit. A force-kill of the bootloader leaks that directory until the private `/tmp`
  is cleared (same as on the `.deb`). The unpacked libraries are executed from `/tmp`, which strict confinement allows.
* Loopback: the control plane binds `127.0.0.1` only. The `network-bind` plug does not widen that.
* The app's own containment backend (`agentic_os/mission/membrane.py`, rlimits in a child process) is unrelated to snap
  confinement and runs inside it; section 6 records whether anything is denied by AppArmor/seccomp.

## 3. Updates: snapd refresh versus the app's own upgrade expectations

* The app has **no self-updater** (no updater plugin; the launcher capability file grants only the app's own commands). Updating is
  done by the package manager: for the snap, snapd refreshes automatically (several times a day, per channel). The store does not let
  a snap opt out of automatic refresh; the user can only hold or schedule refreshes.
* A refresh swaps the whole squashfs revision. The sidecar from the previous revision keeps running until its launcher exits;
  with snapd refresh-app-awareness the refresh of a snap with running apps is deferred and the user is notified. Nothing in the app
  needs to cooperate; the launcher tether (the sidecar exits when the launcher closes) already stops both together.
* Data survives a refresh only if it is in a writable area that snapd carries over (section 4). The wrapper puts the app's
  home-relative stores under `$SNAP_USER_COMMON` for that reason.
* Rollback (`snap revert`) restores `$SNAP_USER_DATA` of the older revision but **not** `$SNAP_USER_COMMON`; a data-format change
  between revisions would therefore need a migration plan before the first public revision. Not designed here.
* The `.deb` upgrade is tested separately in the Linux report (0.1.1 to 0.1.2 over a running app: data preserved, running processes
  keep the replaced binaries until restarted).

## 4. Writable areas

| Location | Owner | Use here |
|---|---|---|
| `$SNAP` | read-only squashfs | launcher, sidecar, `Licenses/` |
| `$SNAP_USER_DATA` (= `$HOME` inside the snap) | per user, per revision, copied by snapd on refresh | launcher WebView storage, cache, the private redirect page |
| `$SNAP_USER_COMMON` | per user, shared by all revisions | `agentic-os/` and `secrets/` (set by the wrapper) |
| `$SNAP_DATA` / `$SNAP_COMMON` | system-wide | unused |
| `/tmp` | private to the snap | sidecar extraction directory |

The snap cannot read or write the user's real home directory, `/var/run/docker.sock`, other users' data, or run `sudo`.

## 5. Architectures and build

* `platforms: amd64, arm64`. Only arm64 was built (native, destructive mode, in the Lima VM). The amd64 build must be made on an
  amd64 builder (Launchpad snap builds, or an `ubuntu-24.04` runner); it will need `packaging/locks/*-linux.lock` for x86_64 (the
  hash lists cover both architectures' wheels; they resolved for aarch64 here) and the x86_64 `cargo-targets/compiled-crates-linux.txt`
  that `build-notices.sh` already assumes (`x86_64-unknown-linux-gnu`).
* The build needs network access for PyPI, GitHub (git pins, uv, tauri-cli's crates, python-build-standalone) and crates.io. Launchpad
  snap builds allow network access by default; a restricted-network builder would need a mirror of those inputs.
* Cross-architecture: the sidecar and launcher are architecture-specific. There is no universal snap.

## 6. Results in the Lima VM (Ubuntu 24.04.5, aarch64)

Build: `sudo snapcraft pack --destructive-mode` (snapcraft 9.2.1, snapd 2.77.1) in a clean clone of branch `rel/linux`, about 14
minutes with a warm network. Result `agentic-apps-r0-prime_0.1.1_arm64.snap`, 117,575,680 bytes, sha256
`a964f72703a902bf2ca8f66fe2fc4c919e58d767c253fd029579b2c89f9abb4f`, version 0.1.1, grade devel, strict. Three attempts were needed;
each failure was a packaging defect fixed in a commit (pip 24.0 leaking in from snapcraft's own environment, git "dubious
ownership" under `--destructive-mode` as root; both in `snap/local/build-payload.sh`). snapcraft lint: only "unused library"
warnings for stage-package libraries.

Install: `snap install --dangerous` works; snapd pulled the content snaps `gnome-46-2404`, `gpu-2404` (mesa-2404) and
`gtk-common-themes` from the store as plug providers (network needed at install). `snap connections agentic-apps-r0-prime`:
`network`, `network-bind`, `desktop`, `desktop-legacy`, `gsettings`, `opengl`, `wayland`, `x11` and the five content plugs; nothing
else, nothing disconnected, nothing needing a manual connect.

Store review tools (`review-tools` 0.48, `review-tools.snap-review`): **pass**; JSON: 0 errors, 0 warnings, 40 info entries,
0 `manual_review` entries (sections `package.v2_squashfs`, `snap.v2_declaration`, `snap.v2_functional`, `snap.v2_lint`,
`snap.v2_security`). The output is `agentic-apps-r0-prime_0.1.1_arm64.snap: pass`. This is the local tool only; the store may apply
more checks at upload.

Control plane inside the strict snap (`snap run --shell`, the sidecar is the one in the snap): serves `127.0.0.1:8787`
only (`ss`), `GET /` 200, `/api/projects` 401 without a session, launch-code exchange works, `bootstrap` prints the same device
report as the `.deb`, SIGTERM of the bootloader leaves nothing running. Confinement is real: `touch /etc/x` denied, no
`/var/run/docker.sock`, the real home directory is not visible, `docker` is not present. The wrapper's data directories were created
under `$SNAP_USER_COMMON` with mode 700. AppArmor/seccomp denials seen while running: `/proc/zoneinfo`, cgroup memory files, DMI
chassis files, the accessibility bus (`org.a11y.Bus`), `sched_setaffinity` and `setpriority` (EPERM): all benign for this app.

Offline (nftables drops every non-loopback packet of one group, the snap runs with that group): loopback `GET /` 200, an external
request fails, the rule counted the dropped packets. (A netns could not be used: `snap run` needs the user's systemd cgroup,
"cannot find tracking cgroup".)

**Blocking finding SNAP-D1: the launcher window is blank under strict confinement in this VM.** `snap run agentic-apps-r0-prime`
under Xvfb opens the 560x640 window after about 30 s, but the WebView never paints the launcher page (flat #fafafa) and "Start &
open" does nothing, so the snap cannot start the control plane from its own GUI. The same launcher from the `.deb` and the AppImage
renders and works in the same VM. Not implicated: AppArmor/seccomp (no denial for the WebKit processes), library versions
(the launcher and the helper processes load the same libwebkit2gtk 2.52 from the snap), compositing/DMA-BUF/GPU
(`WEBKIT_DISABLE_COMPOSITING_MODE`, `WEBKIT_DISABLE_DMABUF_RENDERER`, `WEBKIT_SKIA_ENABLE_CPU_RENDERING`, `LIBGL_ALWAYS_SOFTWARE`
changed nothing). Not tested, because it would weaken a control: disabling WebKit's process sandbox
(`WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS`); `bwrap` itself cannot run inside the snap ("Can't read /proc/sys/kernel/overflowuid").
Root cause is open. Possible next steps, none applied: run the snap on a real GPU desktop (Wayland and X11) to separate a VM-only
rendering problem from a confinement one; try WebKitGTK from the `gnome-46-2404` content snap only (drop the stage-packaged copy);
ask in the snapcraft forum whether WebKitGTK 2.5x needs an interface for the sandbox. The snap is **not shippable** until this is
resolved; the structure, the build, the interface set, the review tools and the control plane are fine.

Uninstall (`snap remove`): processes gone, desktop file removed, `~/snap/agentic-apps-r0-prime` removed and an automatic snapshot of
its data saved (`snap saved`; `snap forget` deletes it, `snap remove --purge` skips it). Left on the system: the content snaps
(`gnome-46-2404`, `gnome-46-2404-sdk`, `gtk-common-themes`, `mesa-2404`: shared, hundreds of MB), `/var/lib/snapd/sequence/agentic-apps-r0-prime.json`,
and the snap-private `/tmp/snap-private-tmp/snap.agentic-apps-r0-prime` directory until reboot.

The amd64 snap, the Wayland session, a real GPU, `snap refresh` from a store channel, `snap revert`, and the store-side checks are
**not tested**.

## 7. What only the owner can do (real Snap Store account; nothing here was attempted)

1. Create or use the Ubuntu One account for the publisher and enable two-factor authentication.
2. Register the snap name (`snapcraft register <name>`); `agentic-apps-r0-prime` in `snap/snapcraft.yaml` is a placeholder. Brand names
   can need a name-claim review. Decide the public name and whether the R0-prime suffix belongs in a store name at all.
3. Request publisher verification if wanted.
4. Create store credentials (`snapcraft login` / exported credentials) in a place only the owner controls; no agent receives them.
5. Decide O-SNAP-1 to O-SNAP-5 (section 2), including any store request for a non-default interface.
6. Fill the store listing: title, summary, description, contact, website, issues and source links, category, screenshots, privacy
   policy URL if any, and the **licence field**. The family licence ("source-available (family licence: AGPL-3.0-or-later with the
   Commons Clause)") has no SPDX identifier; `snapcraft.yaml` leaves `license:` unset on purpose, and the listing must not call the
   product open source. Decision D-LIC-1 in `packaging/distribution-gate.json` stays OPEN.
7. Resolve the distribution gate before any non-private build: every distribution-blocking decision in `packaging/distribution-gate.json`
   is OPEN (18 of the 21 decisions block distribution, all OPEN; D-LIC-13 includes the missing licence texts for libgcc_s, libstdc++ and the
   system WebKitGTK that the Linux build reports in `NOTICES_MISSING.md`) and `REDEVOPS_DISTRIBUTION_BUILD` builds are refused while any is OPEN.
8. Upload (`snapcraft upload`), choose channels (`edge` first), set `grade: stable` only for a stable/candidate release, and answer
   store review. The store review tools run automatically on upload; manual review is triggered by privileged interfaces, classic
   confinement or a name claim.
9. Own the update cadence: every push to a tracked channel is delivered to users automatically.
10. Signing: the Snap Store signs the delivered snap itself. This does not replace the platform signing decisions for the other
    packages and does not make the `.deb`/AppImage trustworthy outside the store.

Nothing was uploaded, registered, released or reviewed by the store; the output in section 6 is from the local review tools only.
