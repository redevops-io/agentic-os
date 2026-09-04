# frozen_string_literal: true
#
# Idempotent seed for the "Meridian Wealth Management" demo tenant on self-hosted Lago v1.48.
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

ORG_NAME = "Meridian Wealth Management"
CURRENCY = "USD"

# --- Organization ------------------------------------------------------------
org = Organization.find_or_initialize_by(name: ORG_NAME)
org.default_currency = CURRENCY
org.country ||= "US"
org.email ||= "billing@meridianwealth.com"
org.save!  # validations on -> slug callback fires

# --- Billing entity (required to attach invoices in v1.48) -------------------
# The first billing entity becomes the org's default_billing_entity.
entity = org.default_billing_entity || org.billing_entities.first
if entity.nil?
  entity = org.billing_entities.create!(
    name: ORG_NAME,
    code: "meridian-wealth",
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
  api_key = org.api_keys.new(name: "meridian-demo")
  api_key.permissions ||= {}
  api_key.save!
end

# --- Customers (advisory clients / households) --------------------------------
# external_id is the natural idempotency key in Lago's REST API.
customers_spec = [
  { external_id: "whitfield-family-trust", name: "Whitfield Family Trust", email: "trustee@whitfieldtrust.example",    type: "company" },
  { external_id: "okonkwo-holdings",       name: "Okonkwo Holdings",       email: "finance@okonkwoholdings.example",   type: "company" },
  { external_id: "delgado-retirement",     name: "Delgado Retirement",     email: "office@delgado.example",            type: "individual" },
  { external_id: "nakamura-foundation",    name: "Nakamura Foundation",    email: "office@nakamurafoundation.example", type: "company" },
  { external_id: "petrov-family-office",   name: "Petrov Family Office",   email: "admin@petrovfamilyoffice.example",  type: "company" }
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
metric.name = "Advisory Reviews"
metric.aggregation_type ||= "count_agg"
metric.recurring = false if metric.recurring.nil?
metric.save!

maint = Plan.find_or_initialize_by(organization_id: org.id, code: "advisory-retainer-monthly")
maint.name = "Advisory Retainer Plan"
maint.interval = "monthly"
maint.amount_cents = 29_900            # $299/mo
maint.amount_currency = CURRENCY
maint.save!

jobs = Plan.find_or_initialize_by(organization_id: org.id, code: "advisory-engagements-oneoff")
jobs.name = "Advisory Engagements (one-off)"
jobs.interval = "monthly"               # interval required even for the one-off engagement template
jobs.amount_cents = 0
jobs.amount_currency = CURRENCY
jobs.save!

# --- Invoices ----------------------------------------------------------------
# `number` is the idempotency key. We hand-build finalized/paid, overdue, and a draft
# so the agent layer has realistic states to act on. Amounts match a mid-size RIA
# (advisory / management fees $2k-$38k+, ~$148k collected MTD).
#
# spec: [number, customer_key, label(engagement), total_dollars, status, payment_status,
#        overdue?, issued_days_ago, due_days_from_issue]
today = Date.current
invoices_spec = [
  # --- Finalized + PAID (collected MTD) ---
  ["MWM-1042", "whitfield-family-trust",  "Whitfield Q2 advisory fee",               14_200, :finalized, :succeeded, false, 18, 30],
  ["MWM-1043", "okonkwo-holdings",        "Okonkwo Holdings management fee (Q2)",    38_500, :finalized, :succeeded, false, 16, 30],
  ["MWM-1044", "nakamura-foundation",     "Nakamura Foundation management fee (Q2)", 27_800, :finalized, :succeeded, false, 14, 30],
  ["MWM-1045", "petrov-family-office",    "Petrov Family Office advisory fee (Q2)",  31_400, :finalized, :succeeded, false, 12, 30],
  ["MWM-1051", "delgado-retirement",      "Delgado Retirement planning fee",          8_000, :finalized, :succeeded, false,  9, 30],
  ["MWM-1054", "whitfield-family-trust",  "Whitfield financial-planning fee",         4_300, :finalized, :succeeded, false,  5, 30],
  ["MWM-1055", "okonkwo-holdings",        "Okonkwo Holdings performance fee",        15_600, :finalized, :succeeded, false,  4, 30],
  ["MWM-1056", "nakamura-foundation",     "Nakamura Foundation tax-loss review",      8_900, :finalized, :succeeded, false,  3, 30],
  # paid total = 14_200+38_500+27_800+31_400+8_000+4_300+15_600+8_900 = 148_700 collected MTD

  # --- OVERDUE (finalized, unpaid, past due) ---
  ["MWM-1048", "delgado-retirement",      "Delgado Retirement advisory fee",          2_300, :finalized, :pending,   true,  10, 4],  # ~6 days overdue
  ["MWM-1049", "petrov-family-office",    "Petrov Family Office management fee",       5_100, :finalized, :pending,   true,  22, 15], # ~7 days overdue

  # --- DRAFT / pending (not yet sent) ---
  ["MWM-1053", "nakamura-foundation",     "Nakamura Foundation IPS proposal (draft)", 6_750, :draft,     :pending,   false,  1, 30]
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
