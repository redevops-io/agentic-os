# QA bundle (redevops-qa): licence hand-off

Hand-off only. `redevops-qa` is a different repository and nothing in it was changed. This note records, from the frozen R0 audit
matrix (`R0_BLOCKER_MATRIX_LICENCE_VERIFICATION.md`, `LICENCE_MATRIX.md`, 2026-11-04) and one re-read, what a QA R0-prime would
need decided. It decides nothing, clears nothing and is not legal advice. Register item: D-LIC-16 (blocks distribution of the QA
bundle only). Quotations in curly double quotation marks are checked against `docs/legal/sources/qa-handoff-facts.txt` by
`tests/test_legal_documents.py`.

## 1. report.css tab styles (HTML Dog): matrix LIC-05, row QA-EV-rf-htmldog-tabs, severity BLOCKER

* What ships: the Robot Framework 7.5 wheel's `robot/htmldata/rebot/report.css` contains tab styles whose comment says they were
  adapted from an HTML Dog article. The wheel is shipped unmodified (`staged_unmodified`), so the rules cannot be removed
  without modifying the wheel.
* Status in the matrix: declared licence “unverified”; owner decision “none yet”; notices shipped: no (no text exists).
* Source terms, re-fetched 2026-11-04 from https://htmldog.com/terms/ (sha256 of the page `6cd966bab56129131def7eb70f2208f27ee9988280a39bdad78c80e487daff9b`, identical to the 2026-11-03 read): “All material on this website is copyright HTML Dog.”; “Content may be quoted if accompanied by a credit to HTML Dog and a link to (or statement of, if the credit is not published on the web) the relevant page on HTML Dog.”; “Material may not be used for profit without the express permission.” The page names no licence.
* Open questions for the owner and counsel: whether those terms bind the short, generic CSS rules in the wheel (the matrix residual says the rules "are short and generic"); whether the attribution comment in the wheel satisfies the quoting condition; whether distribution of the bundle counts as "use for profit".
* Options: (a) ask HTML Dog or Robot Framework in writing for a statement or permission (owner action; nothing was contacted); (b) replace the style rules by a patched wheel (changes a third-party wheel: a pinned-input change with its own notice and test burden); (c) exclude Robot Framework's HTML reports from the bundle if the QA product does not need `report.html`; (d) do not distribute the QA bundle publicly until decided. Recommended default: (d) with (a) in parallel.

## 2. jQuery tablesorter 2.30.5 in Robot Framework's report assets: matrix LIC-12, severity MAJOR

* Declared “(MIT OR GPL-2.0)” by upstream (package.json, composer.json, registry, README "MIT or GPLv2 (pick one)"); the shipped minified file has no header; the fork publishes no licence text; the original `christianbach/tablesorter` MIT text ships, labelled, with the declaration quotes.
* The owner elected MIT conditionally on 2026-11-03 (recorded in the matrix). Not decided: confirmation for public distribution, and extension of the election to the blue-theme styles and icons in `common.css`.
* Options: (a) confirm the MIT election in writing for public distribution and for the `common.css` assets; (b) ask upstream (Mottie) for a licence file; (c) exclude the HTML report assets. Recommended default: (a) after counsel reads the declaration quotes.

## 3. The rest of the QA decision backlog (matrix LIC-04)

24 of 29 evidence items say "none yet"; 5 carry conditional or provisional owner decisions. The items: jQuery Templates (MIT or GPL-2.0, edited upstream, no text), jQuery Highlight (MIT header, second origin unread), JSXCompressor (Apache-2.0 or LGPL-3.0, release unidentified), Robot Framework `highlighting.py` (MIT per a comment), `asserts.py` ("same terms as Python itself"), the Playwright codicon font (MIT folder text versus CC-BY-4.0 upstream), the Playwright dashboard browser logos (no licence for the artwork), six shipped third-party wheels recorded `staged_unmodified` with decision "none yet", the plain-AGPL `agent-harness` tree inside a family-licence bundle (the same question as D-LIC-3), and the public-licence statement that the QA repository's own `LICENSE.md` says is not final. Node.js 24.22.1 is accepted provisionally only (matrix LIC-14).

## 4. What a QA R0-prime needs (a list, not a plan)

1. A written owner decision per matrix row marked "none yet" (the form in `OWNER_DECISION_FORM.md` can be extended with these ids, in the QA repository).
2. The two items above resolved or removed from the distribution.
3. The same source-availability, notice and Appropriate Legal Notices decisions as D-LIC-5, -10, -12 and pins-03 for the QA product, with its own source offer.
4. A statement of which licence the QA bundle carries publicly, after D-LIC-1 is decided for the family.

Nothing here changes `redevops-qa`; the owner decides where the QA decisions are recorded.
