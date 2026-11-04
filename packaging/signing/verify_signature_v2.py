#!/usr/bin/env python3
"""Cross-platform Authenticode inspection/verification using the 'signify' library.   (v2)
Run ONLY as:  <venv>/bin/python -I verify_signature_v2.py [options] FILE...
It never uses the network (no CRL/OCSP fetching).  It does NOT prove Windows acceptance.

Differences from verify_signature.py (v1, kept unchanged):
  * --expect-cn VALUE   EXACT match of the signer certificate's commonName (parsed from the certificate's subject RDNs;
                        the certificate must carry exactly one CN and it must equal VALUE, case-sensitive). The v1 option
                        --expect-subject (substring test on the printed subject) is kept for compatibility but is weak:
                        do not use it for a release gate.
  * a malformed certificate / signature / file no longer ends in a Python traceback: each file is verified inside a
    guard that prints ONE line  'FAIL: <file>: <ExceptionType>: <short message>'  and counts as a failure (exit 1).
    The verifier still fails closed.
  * bad command-line usage prints one line and exits 2.

Options:
  --trust-anchor PEM      verify against ONLY these roots (dry run with the throwaway CA).
                          default: signify's bundled Microsoft trust store (mscerts + CTL data).
  --expect-cn VALUE       require the signer certificate's single CN to equal VALUE exactly.
  --expect-subject STR    (legacy, weak) require STR to be a substring of the printed signer subject.
  --expect-eku OID        require OID in the signer certificate EKU (repeatable).
  --require-timestamp     fail if there is no countersignature (RFC3161) timestamp.
  --at ISO8601            verification time (default: the timestamp-signing time if present, else now).
  --json                  print machine readable result as JSON on stdout (human text goes to stderr).
Exit code 0 only if all requested checks pass.
"""
import sys, json, datetime, hashlib


def usage_error(msg):
    print("usage error: " + msg + " (see --help)", file=sys.stderr)
    sys.exit(2)


try:
    from signify.authenticode import TRUSTED_CERTIFICATE_STORE
    from signify.authenticode.signed_file import SignedPEFile
    from signify.x509 import Certificate, CertificateStore, VerificationContext
    try:
        from signify.authenticode.signed_file import SignedMsiFile
    except ImportError:
        SignedMsiFile = None
except Exception as e:  # missing / broken signify: one line, fail closed
    print("FAIL: cannot import signify: %s: %s" % (type(e).__name__, str(e)[:200]), file=sys.stderr)
    sys.exit(3)


def short(e):
    return str(e).replace("\n", " ")[:200]


def args():
    a = {"files": [], "anchor": None, "subject": None, "cn": None, "eku": [], "ts": False, "at": None, "json": False}
    it = iter(sys.argv[1:])

    def val(opt):
        try:
            return next(it)
        except StopIteration:
            usage_error(opt + " needs a value")

    for x in it:
        if x == "--trust-anchor": a["anchor"] = val(x)
        elif x == "--expect-subject": a["subject"] = val(x)
        elif x == "--expect-cn": a["cn"] = val(x)
        elif x == "--expect-eku": a["eku"].append(val(x))
        elif x == "--require-timestamp": a["ts"] = True
        elif x == "--at":
            v = val(x)
            try:
                a["at"] = datetime.datetime.fromisoformat(v).astimezone(datetime.timezone.utc)
            except Exception:
                usage_error("--at needs an ISO8601 time, got %r" % v[:40])
        elif x == "--json": a["json"] = True
        elif x in ("-h", "--help"): print(__doc__); sys.exit(0)
        elif x.startswith("--"): usage_error("unknown option " + x)
        else: a["files"].append(x)
    if not a["files"]:
        usage_error("no FILE given")
    if a["cn"] is not None and a["cn"] == "":
        usage_error("--expect-cn must not be empty")
    return a


def subject_cns(c):
    """All commonName values of the certificate subject, read from the DER structure (not from a printed string)."""
    out = []
    name = c.asn1["tbs_certificate"]["subject"]
    for rdn in name.chosen:
        for atv in rdn:
            if atv["type"].native == "common_name":
                out.append(atv["value"].native)
    return out


def cert_info(c):
    x = c.asn1
    eku = []; pol = []; ku = []
    try:
        v = x.extended_key_usage_value
        eku = [e.dotted for e in v] if v is not None else []
    except Exception: pass
    try:
        v = x.certificate_policies_value
        pol = [p["policy_identifier"].dotted for p in v] if v is not None else []
    except Exception: pass
    try:
        v = x.key_usage_value
        ku = sorted(v.native) if v is not None else []
    except Exception: pass
    return {
        "subject": str(c.subject.dn), "issuer": str(c.issuer.dn), "common_names": subject_cns(c),
        "serial": format(c.serial_number, "x"), "not_before": c.valid_from.isoformat(), "not_after": c.valid_to.isoformat(),
        "sha1": c.sha1_fingerprint, "sha256": c.sha256_fingerprint,
        "eku_oids": eku, "key_usage": ku, "policy_oids": pol,
    }


def verify_one(fn, a, store, P):
    """returns (result dict, ok bool). May raise: the caller turns any exception into a one-line failure."""
    with open(fn, "rb") as fh:
        data = fh.read()
    res = {"file": fn, "sha256": hashlib.sha256(data).hexdigest(), "checks": {}}
    P("=====", fn)
    P("file sha256:", res["sha256"])
    is_msi = fn.lower().endswith(".msi")
    with open(fn, "rb") as f:
        sf = (SignedMsiFile if is_msi else SignedPEFile)(f)
        try:
            sigs = list(sf.iter_embedded_signatures())
        except Exception as e:
            sigs = []; P("not signed / unparsable:", type(e).__name__)
        res["signature_count"] = len(sigs)
        P("embedded signatures:", len(sigs))
        if not sigs:
            res["checks"]["signed"] = False
            P("checks:", res["checks"], "=> FAIL")
            res["ok"] = False
            return res, False
        sig = sigs[0]
        si = sig.signer_info
        res["digest_algorithm"] = sig.digest_algorithm.__name__ if hasattr(sig.digest_algorithm, "__name__") else str(sig.digest_algorithm)
        res["program_name"] = si.program_name
        res["authenticode_digest"] = sig.indirect_data.digest.hex()
        P("digest algorithm:", res["digest_algorithm"], "| authenticode digest:", res["authenticode_digest"])
        P("program name:", si.program_name)
        signer = None
        for c in sig.certificates:  # signer cert = cert whose issuer+serial match signer_info
            if c.serial_number == si.serial_number and c.issuer == si.issuer:
                signer = c
        res["signer"] = cert_info(signer) if signer else None
        res["certs_in_signature"] = [cert_info(c) for c in sig.certificates]
        P("signer:", res["signer"]["subject"] if signer else None)
        if signer:
            P("  issuer:", res["signer"]["issuer"]); P("  validity:", res["signer"]["not_before"], "->", res["signer"]["not_after"])
            P("  common name(s):", res["signer"]["common_names"])
            P("  EKU:", res["signer"]["eku_oids"], "| policy OIDs:", res["signer"]["policy_oids"])
        cs = si.countersigner
        ts_time = None
        if cs is not None:
            st = getattr(cs, "signing_time", None)
            ts_time = st
            res["timestamp"] = {"type": type(cs).__name__, "signing_time": st.isoformat() if st else None}
            try:
                res["timestamp"]["tsa_certs"] = [str(c.subject.dn) for c in getattr(cs, "certificates", [])]
            except Exception: pass
            P("timestamp countersignature:", res["timestamp"])
        else:
            res["timestamp"] = None
            P("timestamp countersignature: NONE")
        chk = res["checks"]
        if a["ts"]: chk["timestamp_present"] = cs is not None
        if a["cn"] is not None:
            cns = res["signer"]["common_names"] if signer else []
            chk["cn_exact"] = bool(signer) and len(cns) == 1 and cns[0] == a["cn"]
        if a["subject"]: chk["subject_contains_LEGACY"] = bool(signer and a["subject"] in res["signer"]["subject"])
        for oid in a["eku"]: chk["eku_" + oid] = bool(signer and oid in res["signer"]["eku_oids"])
        when = a["at"] or ts_time
        ctxkw = {"timestamp": when} if when else {}
        try:
            chains = list(sig.verify(trusted_certificate_store=store,
                  verification_context_kwargs=dict(ctxkw, revocation_mode="soft-fail", allow_fetching=False),
                  countersignature_mode="permit" if cs is None else "strict"))
            chk["signature_and_chain"] = True
            res["chain"] = [[str(c.subject.dn) for c in ch] for ch in chains][:1]
            P("VERIFY (hash match + signature + chain to trust store): OK")
            P("  chain:", " -> ".join(res["chain"][0]))
        except Exception as e:
            chk["signature_and_chain"] = False; res["verify_error"] = "%s: %s" % (type(e).__name__, e)
            P("VERIFY FAILED:", res["verify_error"])
    ok = all(chk.values()); res["ok"] = ok
    P("checks:", chk, "=>", "PASS" if ok else "FAIL")
    return res, ok


def main():
    a = args()
    ok_all = True
    out = []
    log = sys.stderr if a["json"] else sys.stdout
    P = lambda *x: print(*x, file=log)
    try:
        if a["anchor"]:
            roots = list(Certificate.from_pems(open(a["anchor"], "rb").read()))
            if not roots:
                raise ValueError("no certificate found in the trust anchor file")
            store = CertificateStore(roots, trusted=True)
            P("trust store: ONLY", a["anchor"], "(", len(roots), "cert(s) )")
        else:
            store = TRUSTED_CERTIFICATE_STORE
            P("trust store: signify bundled Microsoft store")
    except Exception as e:
        print("FAIL: cannot load the trust store: %s: %s" % (type(e).__name__, short(e)), file=sys.stderr)
        sys.exit(1)
    for fn in a["files"]:
        try:
            res, ok = verify_one(fn, a, store, P)
        except Exception as e:  # malformed certificate, truncated file, unreadable file, ... -> one line, fail closed
            line = "FAIL: %s: %s: %s" % (fn, type(e).__name__, short(e))
            print(line, file=(sys.stderr if a["json"] else sys.stdout))
            res = {"file": fn, "ok": False, "error": line}; ok = False
        ok_all &= ok
        out.append(res)
    if a["json"]: print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if ok_all else 1)


try:
    main()
except SystemExit:
    raise
except BaseException as e:  # last resort: never a traceback
    print("FAIL: verifier aborted: %s: %s" % (type(e).__name__, short(e)), file=sys.stderr)
    sys.exit(1)
