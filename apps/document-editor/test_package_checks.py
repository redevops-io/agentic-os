"""Package-level active-content checks for docx / xlsx / pptx edit results.

Every test marked [FAILS on feba1a0] exercised a gap in the previous checks (they read only word/document.xml, had no
xlsx/pptx branch, matched TargetMode case-sensitively without strip, and ignored OLE elements without parts).
"""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

APPROVAL = {"approved": True, "by": "alice"}
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def mutate(data: bytes, add: dict = None, edit: dict = None, ct: str = "") -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in zin.namelist():
            c = zin.read(n)
            if edit and n in edit:
                c = edit[n](c.decode()).encode()
            if ct and n == "[Content_Types].xml":
                c = c.decode().replace("</Types>", ct + "</Types>").encode()
            z.writestr(n, c)
        for n, c in (add or {}).items():
            z.writestr(n, c)
    return b.getvalue()


def part(tag, body):
    return f'<w:{tag} {W}><w:p>{body}</w:p></w:{tag}>'


def run_fld(instr):
    return f'<w:r><w:instrText>{instr}</w:instrText></w:r>'


SPLIT = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DD</w:instrText></w:r>'
         '<w:r><w:instrText>EAUTO c:\\\\x</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>')

DOCX_ATTACKS = {
    "header-DDEAUTO": dict(add={"word/header9.xml": part("hdr", run_fld("DDEAUTO c:\\\\windows\\\\cmd.exe"))}),
    "footer-INCLUDETEXT": dict(add={"word/footer9.xml": part("ftr", run_fld("INCLUDETEXT file:///etc/passwd"))}),
    "footnotes-LINK": dict(add={"word/footnotes.xml": part("footnotes", run_fld("LINK Excel.Sheet.8 x y"))}),
    "endnotes-IMPORT": dict(add={"word/endnotes.xml": part("endnotes", run_fld("IMPORT http://evil/x"))}),
    "comments-INCLUDEPICTURE": dict(add={"word/comments.xml": part("comments", run_fld("INCLUDEPICTURE http://evil/p.png"))}),
    "glossary": dict(add={"word/glossary/document.xml": part("glossaryDocument", run_fld("DDE a b"))}),
    "case-variant": dict(add={"word/header9.xml": part("hdr", run_fld("ddeAuto  c:\\\\x"))}),
    "split-fldChar-runs": dict(edit={"word/document.xml": lambda x: x.replace("</w:body>", f"<w:p>{SPLIT}</w:p></w:body>")}),
    "rel-' External'": dict(add={"word/_rels/document.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r9" Type="t" Target="http://e/x" TargetMode=" External"/></Relationships>'}),
    "rel-EXTERNAL-in-header-rels": dict(add={"word/_rels/header1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r9" Type="t" Target="http://e/x" TargetMode="EXTERNAL"/></Relationships>'}),
    "ole-element-no-part": dict(edit={"word/document.xml": lambda x: x.replace("</w:body>", '<w:p><w:r><w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" ProgID="Package"/></w:object></w:r></w:p></w:body>')}),
}


def doctored_apply(env, kit, name, base, ops, mutation, key):
    env.put("acme", name, base, label="PUBLIC")
    p = env.svc.propose_patch("acme", name, ops)
    real = env.backend._do_apply_edit

    def doctored(ref, o, **k):
        out = real(ref, o, **k)
        data = mutate(out.content, **mutation)
        return env.mods.backend.BackendFile(content=data, filename="r", sha256=hashlib.sha256(data).hexdigest())
    env.backend._do_apply_edit = doctored
    return lambda: env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"],
                                       backend=p["backend"], patch_digest=p["patch_digest"],
                                       expected_version=1, idempotency_key=key, approval=APPROVAL)


@pytest.mark.parametrize("attack", sorted(DOCX_ATTACKS))
def test_docx_active_content_in_any_part_is_refused(env, kit, attack):
    """[FAILS on feba1a0] for every case EXCEPT `rel-EXTERNAL-in-header-rels` and `split-fldChar-runs`, which already passed
    there (regression guards for the same class)."""
    go = doctored_apply(env, kit, "a.docx", kit.make_docx(["Price 100 EUR"]),
                        [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}], DOCX_ATTACKS[attack],
                        f"idem-{abs(hash(attack))}-01")
    with pytest.raises(env.mods.errors.VerificationFailed):
        go()
    assert env.store.head("acme", "a.docx").version == 1


def test_docx_preexisting_fields_links_and_parts_are_tolerated_when_unchanged(env, kit):
    """REGRESSION GUARD: only CHANGES are flagged, not what the base already had."""
    base = mutate(kit.make_docx(["Price 100 EUR"]),
                  add={"word/footer1.xml": part("ftr", SPLIT.replace("DD", "PA").replace("EAUTO c:\\\\x", "GE")),
                       "word/_rels/document.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="t" Target="http://ok.example/" TargetMode="External"/></Relationships>',
                       "word/embeddings/old.bin": b"ole"})
    go = doctored_apply(env, kit, "k.docx", base, [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}],
                        {}, "idem-tolerated-01")
    assert go()["status"] == "ok"


# ── xlsx ────────────────────────────────────────────────────────────────────────────────────────────────────────
XLSX_ATTACKS = {
    "externalLinks": dict(add={"xl/externalLinks/externalLink1.xml": "<externalLink/>"}),
    "connections": dict(add={"xl/connections.xml": "<connections/>"}),
    "queryTables": dict(add={"xl/queryTables/queryTable1.xml": "<queryTable/>"}),
    "embeddings": dict(add={"xl/embeddings/oleObject1.bin": b"ole"}),
    "vba": dict(add={"xl/vbaProject.bin": b"vba"},
                ct='<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>'),
    "external-rel": dict(add={"xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="t" Target="file:///c:/x.xlsx" TargetMode=" external "/></Relationships>'}),
    "workbook-external-reference": dict(edit={"xl/workbook.xml": lambda x: x.replace("/>", "><externalReferences><externalReference r:id=\"r1\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"/></externalReferences></workbook>", 1) if x.rstrip().endswith("/>") else x}),
    "dde-ole-link": dict(add={"xl/worksheets/sheet2.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><oleObjects><oleObj progId="Package" dde="1"/></oleObjects></worksheet>'}),
    "activex": dict(add={"xl/activeX/activeX1.xml": "<ax/>"}),
}


@pytest.mark.parametrize("attack", sorted(XLSX_ATTACKS))
def test_xlsx_edit_results_get_the_same_package_checks(env, kit, attack):
    """[FAILS on feba1a0]: there was no xlsx package check at all."""
    base = kit.make_xlsx({"B2": 42})
    out = kit.make_xlsx({"B2": 99})
    env.put("acme", "s.xlsx", base, label="PUBLIC")
    p = env.svc.propose_patch("acme", "s.xlsx", [{"type": "set_cell", "cell": "B2", "value": 99}])
    data = mutate(out, **XLSX_ATTACKS[attack])
    env.backend._do_apply_edit = lambda ref, ops, **k: env.mods.backend.BackendFile(
        content=data, filename="r.xlsx", sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(env.mods.errors.VerificationFailed):
        env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1,
                            idempotency_key=f"idem-x-{abs(hash(attack))}", approval=APPROVAL)
    assert env.store.head("acme", "s.xlsx").version == 1


def test_xlsx_clean_edit_with_preexisting_external_link_is_accepted(env, kit):
    """REGRESSION GUARD: a base that already links externally is not flagged when the link is unchanged."""
    extra = {"xl/externalLinks/externalLink1.xml": "<externalLink/>",
             "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="t" Target="file:///c:/x.xlsx" TargetMode="External"/></Relationships>'}
    base = mutate(kit.make_xlsx({"B2": 42}), add=extra)
    out = mutate(kit.make_xlsx({"B2": 99}), add=extra)
    env.put("acme", "s.xlsx", base, label="PUBLIC")
    p = env.svc.propose_patch("acme", "s.xlsx", [{"type": "set_cell", "cell": "B2", "value": 99}])
    env.backend._do_apply_edit = lambda ref, ops, **k: env.mods.backend.BackendFile(
        content=out, filename="r.xlsx", sha256=hashlib.sha256(out).hexdigest())
    r = env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key="idem-x-keep-01",
                            approval=APPROVAL)
    assert r["status"] == "ok"


# ── pptx (package_problems is format-agnostic; no pptx edit op exists yet) ───────────────────────────────────────
def make_pptx():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        z.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
    return b.getvalue()


PPTX_ATTACKS = {
    "embeddings": dict(add={"ppt/embeddings/oleObject1.bin": b"ole"}),
    "oleObj-element": dict(edit={"ppt/slides/slide1.xml": lambda x: x.replace("/>", '><p:oleObj progId="Package"/></p:sld>', 1)}),
    "slide-rel-external": dict(add={"ppt/slides/_rels/slide1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="t" Target="http://e/x" TargetMode="External "/></Relationships>'}),
    "vba": dict(add={"ppt/vbaProject.bin": b"v"}),
    "ole-rel-type": dict(add={"ppt/slides/_rels/slide1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="o.bin"/></Relationships>'}),
}


@pytest.mark.parametrize("attack", sorted(PPTX_ATTACKS))
def test_pptx_package_problems(mods, attack):
    """[FAILS on feba1a0]: function did not exist / covered docx document.xml only."""
    base = make_pptx()
    assert mods.ooxml.package_problems(base, base) == []
    assert mods.ooxml.package_problems(base, mutate(base, **PPTX_ATTACKS[attack]))


def test_target_mode_is_case_and_whitespace_insensitive(mods):
    """[FAILS on feba1a0]: ' External' was not recognised."""
    base = make_pptx()
    for tm in (" External", "EXTERNAL", "external ", "\tExternal\n"):
        data = mutate(base, add={"ppt/slides/_rels/slide1.xml.rels": f'<Relationships {REL_NS}><Relationship Id="r1" Type="t" Target="http://e/x" TargetMode="{tm}"/></Relationships>'})
        assert mods.ooxml.package_risks(data)["external"] == ["http://e/x"]
        assert mods.ooxml.external_targets(data) == ["http://e/x"]


def test_fields_are_reassembled_across_runs_and_normalised(mods, kit):
    base = kit.make_docx(["x"])
    a = mutate(base, edit={"word/document.xml": lambda x: x.replace("</w:body>", f"<w:p>{SPLIT}</w:p></w:body>")})
    assert mods.ooxml.package_risks(a)["fields"] == ["DDEAUTO C:\\\\X"]
    b = mutate(base, edit={"word/document.xml": lambda x: x.replace("</w:body>", '<w:p><w:fldSimple w:instr="  ddeauto   C:\\\\x "/></w:p></w:body>')})
    assert mods.ooxml.package_risks(b)["fields"] == mods.ooxml.package_risks(a)["fields"]


def test_empty_parts_are_tolerated_but_new_malformed_parts_are_not(mods, kit):
    """Found by the real-server run: ONLYOFFICE emits empty .xml parts."""
    base = kit.make_docx(["x"])
    assert mods.ooxml.package_problems(base, mutate(base, add={"word/empty.xml": b""})) == []
    assert mods.ooxml.package_problems(base, mutate(base, add={"word/bad.xml": b"<a><b></a>"}))
    with_bad = mutate(base, add={"word/bad.xml": b"<a><b></a>"})
    assert mods.ooxml.package_problems(with_bad, with_bad) == []
