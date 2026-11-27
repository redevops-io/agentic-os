"""An independent bank statement against the books' payments: matched, timing, fees, split and partial payments, reversals, unmatched lines, currency, strict parsing."""
from decimal import Decimal

from agentic_os.economics import bank
from agentic_os.economics.contracts import Extraction


def P(name, date, amount, ref, cur="USD"):
    return bank.Posting(name, date, Decimal(amount), cur, bank._ref(ref), "Cedar Foods", ("INV-1",))


def stmt(text):
    ex = Extraction()
    return bank.parse_statement(text.strip(), "s.csv", ex), ex


def kinds(findings):
    return sorted((f.kind, f.reference) for f in findings)


def test_same_reference_amount_and_day_match_and_a_different_day_is_a_timing_difference_with_the_month_end_said():
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,3800,WIRE-1,Cedar\n2026-11-02,500,WIRE-2,Cedar")
    out = bank.match(lines, [P("PE-1", "2026-10-05", "3800", "wire-1"), P("PE-2", "2026-10-29", "500", "WIRE-2")])
    assert kinds(out) == [("matched", "WIRE-1"), ("timing_difference", "WIRE-2")]
    assert "across a month end" in next(f for f in out if f.kind == "timing_difference").note


def test_a_fee_line_is_attributed_only_when_the_arithmetic_closes():
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,3380,W1,wire in\n2026-10-05,-420,W1-FEE,Bank transfer fee")
    out = bank.match(lines, [P("PE-1", "2026-10-05", "3800", "W1")])
    assert kinds(out) == [("matched_with_fee", "W1")] and out[0].difference == Decimal("420.00")
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,3380,W1,wire in\n2026-10-05,-400,W1-FEE,Bank transfer fee")
    out = bank.match(lines, [P("PE-1", "2026-10-05", "3800", "W1")])
    assert ("amount_mismatch", "W1") in kinds(out) and ("unmatched_bank", "W1-FEE") in kinds(out)        # 3380 + 400 != 3800: nothing is forced


def test_several_deposits_that_add_up_are_a_split_payment_and_fewer_are_a_partial_payment():
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,2000,W3,part 1\n2026-10-12,1800,W3,part 2")
    assert kinds(bank.match(lines, [P("PE-3", "2026-10-05", "3800", "W3")])) == [("split_payment", "W3")]
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,2000,W3,part 1\n2026-10-12,1000,W3,part 2")
    f = bank.match(lines, [P("PE-3", "2026-10-05", "3800", "W3")])[0]
    assert (f.kind, f.difference) == ("partial_payment", Decimal("800.00"))


def test_a_debit_naming_a_payments_reference_is_a_reversal_the_books_do_not_have_yet():
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,1200,W4,in\n2026-10-20,-1200,RTN-W4,payment returned")
    out = bank.match(lines, [P("PE-4", "2026-10-05", "1200", "W4")])
    assert ("reversal_not_in_books", "W4") in kinds(out) and not any(f.kind == "unmatched_bank" for f in out)


def test_lines_and_payments_with_no_counterpart_are_unmatched_and_a_late_payment_is_in_transit_not_missing():
    lines, _ = stmt("date,amount,reference,description\n2026-10-05,77,X9,unknown credit\n2026-10-30,10,A,x")
    out = bank.match(lines, [P("PE-5", "2026-10-06", "900", "W5"), P("PE-6", "2026-11-05", "900", "W6")])
    assert ("unmatched_bank", "X9") in kinds(out) and ("unmatched_book", "W5") in kinds(out) and ("in_transit", "W6") in kinds(out)


def test_a_different_currency_is_flagged_and_never_converted():
    lines, _ = stmt("date,amount,reference,description,currency\n2026-10-05,2000,W7,in,EUR")
    f = bank.match(lines, [P("PE-7", "2026-10-05", "2000", "W7", cur="USD")])[0]
    assert f.kind == "amount_mismatch" and "not converted" in f.note


def test_an_unreadable_row_is_a_gap_not_dropped_and_not_zero():
    lines, ex = stmt("date,amount,reference,description\n2026-10-05,3800,W1,ok\n05/10/2026,3800,W2,bad date\n2026-10-06,12x,W3,bad amount")
    assert len(lines) == 1 and len(ex.gaps) == 2 and "row 3" in ex.gaps[0]["source"]
    ex2 = Extraction()
    assert bank.parse_statement("foo,bar\n1,2", "t.csv", ex2) == [] and ex2.gaps


def test_deposit_and_withdrawal_columns_are_accepted_and_a_payment_with_no_reference_cannot_be_matched():
    lines, _ = stmt("date,deposit,withdrawal,reference,description\n2026-10-05,100,,A1,in\n2026-10-06,,20,A2,out")
    assert [l.amount for l in lines] == [Decimal("100.00"), Decimal("-20.00")]
    out = bank.match([], [P("PE-8", "2026-10-06", "50", "")])
    assert out[0].kind == "unmatched_book" and "no reference" in out[0].note
