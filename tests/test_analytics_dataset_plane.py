"""Cross-app analytical dataset plane — entity-resolved join with provenance.

Proves BI becomes genuinely cross-app: observations from CRM + support + billing are joined on a resolved entity
into one dataset; each row carries provenance (which systems, freshness, resolution status); a same-key duplicate
in one source is FLAGGED as conflicted (never silently merged); and a stale/failed source makes the dataset's
freshness honest rather than silently serving old data.
"""
from __future__ import annotations

from agentic_os.analytics import (
    ColumnSpec, DatasetDefinition, DatasetMode, DatasetRefreshPolicy, Freshness, InMemoryDatasetStore,
    SourceObservations, materialize,
)

NOW = 1_800_000_000_000
_DAY = 86_400_000


def _defn(mode=DatasetMode.SNAPSHOT, sla_s=0):
    return DatasetDefinition(
        name="customer_360", entity_type="account", key_field="email",
        columns=(
            ColumnSpec("email"),                                                   # identity column
            ColumnSpec("arr", source_resource="crm", source_field="arr", metric_id="revenue.invoiced"),
            ColumnSpec("open_tickets", source_resource="support", source_field="open"),
            ColumnSpec("mrr", source_resource="billing", source_field="mrr"),
        ),
        refresh=DatasetRefreshPolicy(mode=mode, freshness_sla_s=sla_s, interval_s=sla_s))


def _sources(*, crm_fresh=Freshness.FRESH, crm_dupe=False, crm_observed=NOW):
    crm_rows = [{"id": "c1", "email": "a@acme.com", "arr": 50000}, {"id": "c2", "email": "b@beta.com", "arr": 12000}]
    if crm_dupe:
        crm_rows.append({"id": "c3", "email": "a@acme.com", "arr": 99999})          # a SECOND CRM account, same email
    return [
        SourceObservations("crm", tuple(crm_rows), key_field="email", system="salesforce",
                           observed_at=crm_observed, freshness=crm_fresh),
        SourceObservations("support", ({"id": "t1", "email": "a@acme.com", "open": 3},), key_field="email",
                           system="chatwoot", observed_at=NOW),
        SourceObservations("billing", ({"id": "b1", "email": "a@acme.com", "mrr": 4000},
                                       {"id": "b2", "email": "b@beta.com", "mrr": 1000}), key_field="email",
                           system="lago", observed_at=NOW),
    ]


def test_cross_app_join_with_provenance():
    ds = materialize(_defn(), _sources(), now_ms=NOW)
    assert ds.columns == ("email", "arr", "open_tickets", "mrr")
    acme = [r for r in ds.rows if r.key == "a@acme.com"][0]
    # columns composed from THREE different apps on one resolved entity
    assert acme.values == {"email": "a@acme.com", "arr": 50000, "open_tickets": 3, "mrr": 4000}
    assert set(acme.prov.source_systems) == {"salesforce", "chatwoot", "lago"}
    assert acme.prov.entity_resolution == "resolved" and acme.prov.freshness is Freshness.FRESH
    # beta is only in CRM + billing (no support row) → that column is None, provenance names 2 systems
    beta = [r for r in ds.rows if r.key == "b@beta.com"][0]
    assert beta.values["open_tickets"] is None and set(beta.prov.source_systems) == {"salesforce", "lago"}


def test_same_key_duplicate_is_conflicted_not_merged():
    ds = materialize(_defn(), _sources(crm_dupe=True), now_ms=NOW)
    acme = [r for r in ds.rows if r.key == "a@acme.com"][0]
    assert acme.prov.entity_resolution == "conflicted"       # two CRM accounts, one email → flagged
    assert "a@acme.com" in ds.conflicts
    # the row is still present (not dropped) and flagged in the flat projection
    rec = [r for r in ds.to_records() if r["email"] == "a@acme.com"][0]
    assert rec["_resolution"] == "conflicted"


def test_stale_source_makes_dataset_freshness_honest():
    ds = materialize(_defn(), _sources(crm_fresh=Freshness.STALE), now_ms=NOW)
    acme = [r for r in ds.rows if r.key == "a@acme.com"][0]
    assert acme.prov.freshness is Freshness.STALE            # worst-of its contributing sources
    assert ds.freshness is Freshness.STALE
    assert all(r["_freshness"] == "stale" for r in ds.to_records() if r["email"] == "a@acme.com")


def test_sla_ages_a_fresh_source_to_stale():
    # CRM observed 10 days ago, SLA 1 day → SLA demotes it to STALE even though it reported FRESH
    ds = materialize(_defn(sla_s=_DAY // 1000), _sources(crm_observed=NOW - 10 * _DAY), now_ms=NOW)
    acme = [r for r in ds.rows if r.key == "a@acme.com"][0]
    assert acme.prov.freshness is Freshness.STALE


def test_store_staleness():
    store = InMemoryDatasetStore()
    defn = _defn(mode=DatasetMode.REFRESHED, sla_s=_DAY // 1000)
    ds = materialize(defn, _sources(), now_ms=NOW)
    store.put(ds, definition=defn)
    assert store.get("customer_360") is ds and store.list() == ["customer_360"]
    assert store.is_stale("customer_360", now_ms=NOW) is False
    assert store.is_stale("customer_360", now_ms=NOW + 2 * _DAY) is True   # past the interval
    assert store.is_stale("missing") is True
