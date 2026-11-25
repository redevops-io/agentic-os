"""Sidekick explains a sealed Vault and its recovery, and never asks for a key."""
from agentic_os.stack_knowledge import answer_stack_question


def test_a_sealed_vault_question_gets_the_incident_and_recovery_answer():
    for q in ("Vault is sealed, what do I do?", "why did vault restart and stay sealed", "how do I unseal vault",
              "Explain the Vault incident and how to recover"):
        e = answer_stack_question(q)
        assert e is not None and e.id == "vault-sealed", q
    a = answer_stack_question("vault is sealed").answer
    assert "vault operator unseal" in a and "Never paste an unseal key" in a and "auto-unseal" in a


def test_other_secret_questions_keep_their_own_answer():
    assert answer_stack_question("which secret manager is used in production").id == "production-secrets"
    assert answer_stack_question("where are my secrets stored?").id == "secrets-where"
