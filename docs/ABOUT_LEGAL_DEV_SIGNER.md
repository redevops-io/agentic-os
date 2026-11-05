# Development-only signer for the About / Legal decision-signature mechanism

Status: private development. This page describes a test harness. It is not a signing procedure, it selects no production signer, it
decides nothing and it contains no legal text. Nothing here says that anyone has approved any text, decision or build.

## Purpose

The About page has five legal slots (see [ABOUT_LEGAL_UI.md](ABOUT_LEGAL_UI.md)). A slot is shown as an approved text only when the
decision record and the text carry a valid detached signature (an SSHSIG in the namespace `redevops-legal-decision@v1`) made by a key
listed in `packaging/legal-allowed-signers`, the key's fingerprint is pinned in `legal_signer_fingerprints` of
`packaging/distribution-gate.json`, and the signed record names the same decision, option, slot and text hash as the gate entry.

In the repository both lists are empty and all 21 decisions are OPEN, so that path cannot run on real data. To test the mechanism
without choosing a production signer, `packaging/notices/tools/dev_signer_e2e.py` builds a fixture repository root in a temporary
directory, signs SYNTHETIC records and SYNTHETIC placeholder texts with a DISPOSABLE key, and runs the real gate, the real About
generator, the real checker and the real sidecar code against that fixture. Nothing is mocked.

```
python3 packaging/notices/tools/dev_signer_e2e.py        # or packaging/notices/tools/dev_signer_e2e.sh (env -i wrapper)
python3 -m pytest tests/test_dev_signer_e2e.py           # the same harness as tests (about 15 s, offline)
```

Exit codes: 0 every scenario gave the expected outcome and the production state was untouched; 1 an outcome differed; 4 the harness
refused to start.

## What the harness does

1. Asserts the PRODUCTION state and prints it: `packaging/legal-allowed-signers` lists no key (its sha256 is printed), `legal_signer_fingerprints`
   is `[]`, all 21 decisions of `packaging/distribution-gate.json` are OPEN and carry no decision fields, `docs/legal/approved` and
   `docs/legal/decisions` hold no file, and no private key file exists anywhere in the repository (files that start with a
   `-----BEGIN ... PRIVATE KEY-----` line, or are named like an OpenSSH key file). If any of this is false the harness refuses to run (exit 4)
   before it creates anything.
2. Creates a temporary directory (mode 0700) outside the repository; `--tmp-base` pointing inside the repository, or inside a `.ssh` or
   `.gnupg` directory, is refused.
3. Generates two throwaway ed25519 keys INSIDE that directory with
   `ssh-keygen -t ed25519 -N '' -C 'DEV-ONLY disposable signer'` (file mode 0600 checked, a throwaway `HOME`, never `~/.ssh`, never loaded into an
   agent: every child process gets an explicit minimal environment without `SSH_AUTH_SOCK` and `SSH_AGENT_PID`, and `ssh-add` is never run). Key 1 is
   allow-listed in the fixture only; key 2 is allow-listed nowhere.
4. Builds the fixture root from byte-identical copies of the real code and product files, a copy of the gate file whose 21 decisions are
   marked DECIDED with the label `[DEV FIXTURE]` (in the fixture only), the fixture allowed-signers file (the public half of key 1), the fixture
   `legal_signer_fingerprints`, 21 synthetic decision records (machine-readable header, then "DEV FIXTURE RECORD - NOT A DECISION") and 5
   synthetic approved texts whose whole content is "DEV FIXTURE TEXT - NOT LEGAL TEXT". Every record and text is signed with
   `ssh-keygen -Y sign -n redevops-legal-decision@v1`.
5. For each scenario below, copies the fixture, applies one change and runs, as separate `python -I` processes, the real
   `check-distribution-gate.py --about-slots` (private and `--distribution`), the real `generate_about.py` (private and `--distribution`) and
   the real `check_about.py` (private, and `--distribution` where a distribution record was written). The About record of each scenario is
   also checked with the shared validator `agentic_os/about_record.py` `validate_record`, the contract the launcher view is built on.
6. For four scenarios, starts the sidecar from source (`agentic_os.projects_api.mounted_app`) in-process on an ephemeral 127.0.0.1 port
   and fetches `/about/`, `/about/about.json` and the viewer endpoint `/about/files/licence`, and checks what is displayed per slot: an
   approved-verified slot shows exactly the fixture text with the tag "Approved text, signature verified"; an unverified slot shows its
   text only after the label "Unverified draft, not an approved text."; a pending slot shows the neutral placeholder and its text file is
   never opened. (The in-browser rendering of these states is covered by the browser-gated `tests/test_about_browser.py`, which uses its own
   throwaway key.)
7. In a `finally` block deletes both key files by name, then every other file of the temporary directory individually and then the
   directories (no recursive delete), prints that the keys were deleted, and asserts again that the production state is byte-for-byte what it
   was at the start (allowed-signers hash, gate-file hash, working-tree status, no key file in the repository).

## Outcome table

Output of one run (see the harness output for the per-scenario detail lines). `AV` approved-verified, `UD` unverified-draft, `PD` pending,
`NA` not-applicable; the number is the count of slots out of five. Exit codes: gate 0 allowed, 1 distribution refused, 2 malformed or
mismatching input; About generator 3 = distribution refused.

| Scenario (one change to the all-signed fixture) | Gate, private build | Gate, distribution build | About, private | About, distribution |
|---|---|---|---|---|
| all-signed | 0 | 0 | 0, AV:5 | 0 |
| one byte of a signed record changed (entry hash not updated) | 2 | 2 | 2, no record | 2 |
| one byte of an approved text changed (entry hash not updated) | 2 | 2 | 2, no record | 2 |
| record byte changed and its entry hash rewritten | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| text byte changed and its entry hash rewritten | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| signed by a second throwaway key that is not allow-listed | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| the signed record of one decision reused for another decision | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| chosen option in the entry differs from the signed record | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| signed in another namespace | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| record `.sig` missing | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| text `.sig` missing | 0 + warning | 1 | 0, AV:4 UD:1 | 3 |
| `legal_signer_fingerprints` names another key than the allowed-signers file | 0 + warning | 1 | 0, UD:5 | 3 |
| Section 13 slot recorded as "no statement", signed | 0 | 0 | 0, AV:4 NA:1 | 0 |
| one slot decision left OPEN | 0 | 1 | 0, AV:4 PD:1 | 3 |

Reading it: a hash that no longer matches is caught by the consistency check (exit 2); an attacker who also rewrites the hash still fails
the signature check, and the affected slot is shown only as a labelled draft in a private build and refuses a distribution build. Only the
all-signed fixture (and the signed "no statement" variant) pass a distribution gate. A signature proves which key signed the exact bytes
and nothing else; the harness proves that the checks reject what they should, not that any decision is right.

## Why this signer must never become a production signer

* The private key exists for the length of one run, in a temporary directory, with no passphrase and no custody. It has no identity behind
  it and no owner; its public half is written only into the fixture. Anything it signed is synthetic and is deleted with it.
* A production signature is meant to say that a named person decided something. A key that nobody holds says nothing, and a fixture
  that is "DECIDED" says nothing about any real decision.
* The harness refuses to run if the production allowed-signers file lists a key, if the production fingerprint list is not empty or if
  any production decision is not OPEN. The fixture files carry the label `[DEV FIXTURE]`, and the repository never contains a record,
  a text or a key produced by it.

Do not copy the fixture's allowed-signers line, its fingerprint or any of its files into `packaging/`. Do not change the harness to keep
its key, to write into the repository or to use a key from `~/.ssh` or an agent.

## How production signer selection will differ

This is a procedure for the owner; none of it has been done and none of it is done by these tools.

1. The owner chooses their own key (hardware-backed where they choose) on their own machine. The tools never create, choose or handle it.
2. The owner confirms the key's SHA256 fingerprint through an independent channel (for example their published key page), not through
   the repository.
3. The owner adds the public key to `packaging/legal-allowed-signers` and its fingerprint to `legal_signer_fingerprints`, in a change they
   review (see `docs/legal/CODEOWNERS.proposed`).
4. The owner signs each decision record and each approved text with that key in the namespace `redevops-legal-decision@v1`
   (`ssh-keygen -Y sign -f <key> -n redevops-legal-decision@v1 <file>`), after the decision itself has been recorded through
   `docs/legal/OWNER_DECISION_FORM.md`.
5. Only then can a decision be marked DECIDED; until then the production decisions stay OPEN and the slots stay placeholders.

## Limits

* The harness shows that the mechanism accepts what it should and rejects the listed changes. It does not test every possible
  forgery; the reviewed limits are in [ABOUT_LEGAL_UI.md](ABOUT_LEGAL_UI.md) ("Known limits").
* It runs on a machine with OpenSSH `ssh-keygen`; the sidecar part also needs the project's Python dependencies.
* The Rust launcher's own validation of the record is not exercised here; the Python validator it mirrors is.
