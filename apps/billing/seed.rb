# frozen_string_literal: true
#
# Idempotent seed for the "Summit Roofing Co." demo tenant on self-hosted Lago v1.48.
#
# Run via the Lago API (Rails) container — this is the MOST RELIABLE bootstrap path on
# self-hosted Lago because the org + API key can be created directly without going
# through the GraphQL signUp flow:
#
#   sudo docker exec lago-api bundle exec rails runner /path/inside/container/seed.rb
#
# seed.py copies this file into the container and invokes it. Re-running is safe:
# every record is found-or-created by a natural key (org name, customer external_id,
# plan code, invoice number), so a second run updates in place and never duplicates.
#
# NOTE: we save WITH validations on (normal save!) so Lago's before_validation
# callbacks fire — those generate the NOT-NULL `slug` columns and sequence ids.
# (save!(validate: false) skips them and trips a NotNullViolation on `slug`.)
#
# On success it prints:
#   SEED_OK ...
#   API_KEY=<value>
# so the caller can capture the key the REST API needs (Authorization: Bearer <value>).

ORG_NAME = "Summit Roofing Co."
CURRENCY = "USD"

# --- Organization ------------------------------------------------------------
org = Organization.find_or_initialize_by(name: ORG_NAME)
org.default_currency = CURRENCY
org.country ||= "US"
org.email ||= "billing@summitroofing.example"
org.save!  # validations on -> slug callback fires

# --- Billing entity (required to attach invoices in v1.48) -------------------
# The first billing entity becomes the org's default_billing_entity.
entity = org.default_billing_entity || org.billing_entities.first
if entity.nil?
  entity = org.billing_entities.create!(
    name: ORG_NAME,
    code: "summit-roofing",
    default_currency: CURRENCY,
    country: "US"
  )
end

# --- API key (separate ApiKey model in v1.48) --------------------------------
# In v1.48 the ApiKey `value` is auto-generated (a UUID) by the model on create and
# cannot be set to a chosen string. So we find-or-create ONE key for the org and read
# its generated value back — re-runs reuse the same key, keeping the value stable.
api_key = org.api_keys.first
if api_key.nil?
  api_key = org.api_keys.new(name: "summit-demo")
  api_key.permissions ||= {}
  api_key.save!
end

# --- Customers ---------------------------------------------------------------
# external_id is the natural idempotency key in Lago's REST API.
customers_spec = [
  { external_id: "henderson-residence",    name: "Henderson Residence",     email: "owner@henderson.example",         type: "individual" },
  { external_id: "oak-park-hoa",            name: "Oak Park HOA",            email: "board@oakparkhoa.example",        type: "company" },
  { external_id: "maple-street-commercial", name: "Maple Street Commercial", email: "ap@maplestreet.example",          type: "company" },
  { external_id: "riverside-apartments",    name: "Riverside Apartments",    email: "billing@riverside.example",       type: "company" },
  { external_id: "downtown-retail-llc",     name: "Downtown Retail LLC",     email: "accounts@downtownretail.example", type: "company" }
]

customers = {}
customers_spec.each do |c|
  rec = Customer.find_or_initialize_by(organization_id: org.id, external_id: c[:external_id])
  rec.name = c[:name]
  rec.email = c[:email]
  rec.currency = CURRENCY
  rec.country = "US"
  rec.customer_type = c[:type]
  rec.billing_entity_id = entity.id if rec.respond_to?(:billing_entity_id)
  rec.net_payment_term ||= 30
  rec.save!
  customers[c[:external_id]] = rec
end

# --- Billable metric + plans -------------------------------------------------
metric = BillableMetric.find_or_initialize_by(organization_id: org.id, code: "service_visits")
metric.name = "Service Visits"
metric.aggregation_type ||= "count_agg"
metric.recurring = false if metric.recurring.nil?
metric.save!

maint = Plan.find_or_initialize_by(organization_id: org.id, code: "roof-maintenance-monthly")
maint.name = "Roof Maintenance Plan"
maint.interval = "monthly"
maint.amount_cents = 29_900            # $299/mo
maint.amount_currency = CURRENCY
maint.save!

jobs = Plan.find_or_initialize_by(organization_id: org.id, code: "roofing-jobs-oneoff")
jobs.name = "Roofing Jobs (one-off)"
jobs.interval = "monthly"               # interval required even for the one-off job template
jobs.amount_cents = 0
jobs.amount_currency = CURRENCY
jobs.save!

# --- Invoices ----------------------------------------------------------------
# `number` is the idempotency key. We hand-build finalized/paid, overdue, and a draft
# so the agent layer has realistic states to act on. Amounts match a roofing SME
# (jobs $2k-$15k+, ~$148k collected MTD).
#
# spec: [number, customer_key, label(job), total_dollars, status, payment_status,
#        overdue?, issued_days_ago, due_days_from_issue]
today = Date.current
invoices_spec = [
  # --- Finalized + PAID (collected MTD) ---
  ["SUMMIT-1042", "henderson-residence",     "Henderson asphalt re-roof",           14_200, :finalized, :succeeded, false, 18, 30],
  ["SUMMIT-1043", "oak-park-hoa",             "Oak Park HOA clubhouse re-roof",      38_500, :finalized, :succeeded, false, 16, 30],
  ["SUMMIT-1044", "riverside-apartments",    "Riverside bldg C tear-off + shingle", 27_800, :finalized, :succeeded, false, 14, 30],
  ["SUMMIT-1045", "downtown-retail-llc",      "Downtown Retail TPO flat-roof",       31_400, :finalized, :succeeded, false, 12, 30],
  ["SUMMIT-1051", "maple-street-commercial",  "Commercial flat-roof deposit",         8_000, :finalized, :succeeded, false,  9, 30],
  ["SUMMIT-1054", "henderson-residence",      "Henderson skylight flashing",          4_300, :finalized, :succeeded, false,  5, 30],
  ["SUMMIT-1055", "oak-park-hoa",             "Oak Park gutter system replacement",  15_600, :finalized, :succeeded, false,  4, 30],
  ["SUMMIT-1056", "riverside-apartments",    "Riverside emergency leak repair",       8_900, :finalized, :succeeded, false,  3, 30],
  # paid total = 14_200+38_500+27_800+31_400+8_000+4_300+15_600+8_900 = 148_700 collected MTD

  # --- OVERDUE (finalized, unpaid, past due) ---
  ["SUMMIT-1048", "maple-street-commercial",  "Maple St gutters",                     2_300, :finalized, :pending,   true,  10, 4],  # ~6 days overdue
  ["SUMMIT-1049", "downtown-retail-llc",      "Downtown storefront fascia repair",    5_100, :finalized, :pending,   true,  22, 15], # ~7 days overdue

  # --- DRAFT / pending (not yet sent) ---
  ["SUMMIT-1053", "riverside-apartments",     "Elm Ave tear-off + felt (estimate)",   6_750, :draft,     :pending,   false,  1, 30]
]

invoices_spec.each do |number, cust_key, _label, dollars, status, pay_status, overdue, issued_ago, due_in|
  cust = customers[cust_key]
  total_cents = dollars * 100
  issuing = today - issued_ago
  due = issuing + due_in

  inv = Invoice.find_or_initialize_by(organization_id: org.id, number: number)
  inv.customer = cust
  inv.billing_entity_id = entity.id if inv.respond_to?(:billing_entity_id)
  inv.currency = CURRENCY
  inv.invoice_type = :one_off
  inv.status = status
  inv.payment_status = pay_status
  inv.payment_overdue = overdue
  inv.issuing_date = issuing
  inv.payment_due_date = due
  inv.net_payment_term = due_in
  inv.fees_amount_cents = total_cents
  inv.sub_total_excluding_taxes_amount_cents = total_cents
  inv.sub_total_including_taxes_amount_cents = total_cents
  inv.total_amount_cents = total_cents
  inv.taxes_amount_cents = 0
  inv.taxes_rate = 0
  inv.total_paid_amount_cents = (pay_status == :succeeded ? total_cents : 0)
  inv.finalized_at = (status == :finalized ? issuing.to_time : nil)
  inv.ready_for_payment_processing = (status == :finalized && pay_status != :succeeded)
  inv.save!
end

paid = Invoice.where(organization_id: org.id,
                     payment_status: Invoice.payment_statuses[:succeeded],
                     status: Invoice.statuses[:finalized])
collected = paid.sum(:total_amount_cents) / 100.0
overdue_ct = Invoice.where(organization_id: org.id, payment_overdue: true).count

puts "SEED_OK org=#{org.id} customers=#{Customer.where(organization_id: org.id).count} " \
     "invoices=#{Invoice.where(organization_id: org.id).count} " \
     "collected_mtd=#{collected} overdue=#{overdue_ct}"
puts "API_KEY=#{api_key.value}"
