"""Is a difference explained by a fee, FX, a partial settlement, an allocation, a reversal or timing: independent evidence only; a customer's claim explains nothing."""
from decimal import Decimal

from agentic_os.economics import bank, causes
from agentic_os.economics.contracts import Extraction


def P(amount, ref="W1", unalloc="0", cur="USD"):
    return bank.Posting("PE-1", "2026-10-05", Decimal(amount), cur, ref, "Cedar", ("S5",), Decimal(unalloc))


def fs(text, p):
    ex = Extraction()
    return bank.match(bank.parse_statement(text.strip(), "s.csv", ex), [p])


def kinds(e):
    return {c.kind: c.status for c in e.causes}


def test_without_a_statement_every_cause_is_unknown_and_nothing_is_corroborated():
    e = causes.explain_difference("W1", Decimal("420"), P("3800"), [])
    assert set(kinds(e).values()) == {"unknown"} and not e.corroborated and not e.explained and e.residual == Decimal("420")


def test_a_fee_line_on_the_statement_explains_the_difference_to_the_cent_and_nothing_is_left_to_correct():
    st = fs("date,amount,reference,description\n2026-10-05,3380,W1,in\n2026-10-05,-420,W1-FEE,Bank transfer fee", P("3800"))
    e = causes.explain_difference("W1", Decimal("420"), P("3800"), st)
    assert kinds(e)["fee"] == "supported" and e.explained and e.residual == Decimal("0.00") and not e.corroborated


def test_a_customers_statement_of_a_fee_is_a_claim_and_does_not_explain_it_but_does_not_stop_the_statement_corroborating_the_amount():
    st = fs("date,amount,reference,description\n2026-10-05,3380,W1,in", P("3800"))
    class Claim:
        communication = "chatwoot:7/1"
    e = causes.explain_difference("W1", Decimal("420"), P("3800"), st, [Claim()])
    assert kinds(e)["fee"] == "claimed" and not e.explained and e.residual == Decimal("420.00")
    assert e.corroborated is True                       # the statement independently shows 3380 received, so the books' 3800 is wrong; the claim says only WHO bears the 420, and stays a claim


def test_only_a_statement_that_shows_another_amount_with_every_cause_excluded_corroborates_it():
    st = fs("date,amount,reference,description\n2026-10-05,3380,W1,in", P("3800"))
    e = causes.explain_difference("W1", Decimal("420"), P("3800"), st)
    assert set(kinds(e).values()) == {"excluded"} and e.corroborated and not e.explained and e.residual == Decimal("420.00")


def test_a_different_currency_a_partial_settlement_an_unallocated_remainder_a_reversal_and_a_late_posting_each_account_for_it_or_block_it():
    fx = fs("date,amount,reference,description,currency\n2026-10-05,3380,W1,in,EUR", P("3800"))
    e = causes.explain_difference("W1", Decimal("420"), P("3800"), fx)
    assert kinds(e)["fx"] == "supported" and not e.corroborated
    part = fs("date,amount,reference,description\n2026-10-05,2000,W1,a\n2026-10-12,1000,W1,b", P("3800"))
    e = causes.explain_difference("W1", Decimal("800"), P("3800"), part)
    assert kinds(e)["partial_settlement"] == "supported" and e.explained
    e = causes.explain_difference("W1", Decimal("420"), P("3800", unalloc="420"), fs("date,amount,reference,description\n2026-10-05,3380,W1,in", P("3800")))
    assert kinds(e)["allocation"] == "supported" and e.explained and not e.corroborated
    rev = fs("date,amount,reference,description\n2026-10-05,1200,W1,in\n2026-10-20,-1200,RTN-W1,returned", P("1200"))
    e = causes.explain_difference("W1", Decimal("1200"), P("1200"), rev)
    assert kinds(e)["reversal"] == "supported" and not e.corroborated
    late = causes.explain_difference("W1", Decimal("0"), P("3800"), fs("date,amount,reference,description\n2026-10-09,3800,W1,in", P("3800")))
    assert kinds(late)["timing"] == "supported"
