# Idempotent seed for the Summit Roofing Co. demo tenant on self-hosted Chatwoot.
# Run via:  rails runner /tmp/summit_support_seed.rb   (inside the chatwoot rails container)
#
# Creates / updates (all by stable natural keys, safe to re-run):
#   * a super-admin User (the agent),
#   * the Account "Summit Roofing Co.",
#   * an API channel inbox,
#   * 2 contacts,
#   * ~7 roofing-support conversations across open / pending / resolved,
#   * an inbound message on each so the queue/feed has real subjects.
#
# Prints on success:
#   SEED_OK account=<id> inbox=<id> contacts=<n> conversations=<n> open=<n> ...
#   ACCOUNT_ID=<id>
#   ACCESS_TOKEN=<user.access_token.token>

ADMIN_EMAIL    = 'admin@summitroofing.test'
ADMIN_NAME     = 'Summit Support Agent'
ADMIN_PASSWORD = 'SummitRoof!2026'
ACCOUNT_NAME   = 'Summit Roofing Co.'
INBOX_NAME     = 'Summit Roofing Support'

# --- 1. super-admin user (the agent we hand an access token to) --------------
user = User.find_by(email: ADMIN_EMAIL)
unless user
  user = User.new(
    name: ADMIN_NAME,
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
    password_confirmation: ADMIN_PASSWORD
  )
  user.confirmed_at = Time.current
  user.skip_confirmation! if user.respond_to?(:skip_confirmation!)
  user.save!
end
# Promote to super admin (platform-level). Idempotent.
user.update!(type: 'SuperAdmin') unless user.type == 'SuperAdmin'

# --- 2. account --------------------------------------------------------------
account = Account.find_by(name: ACCOUNT_NAME) || Account.create!(name: ACCOUNT_NAME, locale: 'en')

# Link the user to the account as administrator (the agent identity for the API).
AccountUser.find_or_create_by!(account: account, user: user) do |au|
  au.role = :administrator
end
# Ensure role is administrator even if the row pre-existed.
au = AccountUser.find_by(account: account, user: user)
au.update!(role: :administrator) unless au.administrator?

# --- 3. API channel inbox ----------------------------------------------------
channel = Channel::Api.find_by(account: account) ||
          Channel::Api.create!(account: account, webhook_url: '')
inbox = Inbox.find_by(account: account, name: INBOX_NAME)
unless inbox
  inbox = Inbox.create!(account: account, name: INBOX_NAME, channel: channel)
end
# Make sure the agent is a member of the inbox so it can be assigned.
InboxMember.find_or_create_by!(inbox: inbox, user: user)

# --- 4. contacts -------------------------------------------------------------
def upsert_contact(account, name, email, phone)
  c = Contact.find_by(account: account, email: email)
  c ||= Contact.create!(account: account, name: name, email: email, phone_number: phone)
  c
end

henderson = upsert_contact(account, 'Dana Henderson', 'dana.henderson@example.com', '+15125550142')
maple     = upsert_contact(account, 'Marcus Webb',     'marcus.webb@example.com',    '+15125550199')

# --- 5. conversations (idempotent by a deterministic identifier) -------------
# Each entry: contact, status, priority, channel-ish source label, inbound text.
TICKETS = [
  { key: 'quote-2200-reroof', contact: :henderson, status: :open,     priority: :medium,
    source: 'Website',
    body: "Hi — I'd like a quote for a full re-roof on my 2,200 sqft house in Henderson. Existing is old asphalt shingle. What's your availability and ballpark cost?" },
  { key: 'reschedule-rain',   contact: :maple,     status: :open,     priority: :low,
    source: 'Phone',
    body: "It's supposed to rain Tuesday when your crew is scheduled. Can we push the install to Thursday instead?" },
  { key: 'warranty-ridge-leak', contact: :henderson, status: :pending, priority: :high,
    source: 'Email',
    body: "We're seeing a small leak near the ridge after last week's storm — the Oak Park job you did in spring. I believe this is under warranty. Can someone come look?" },
  { key: 'invoice-question',  contact: :maple,     status: :pending,  priority: :medium,
    source: 'Email',
    body: "Quick question on invoice #1048 — there's a $300 line item for 'tear-off disposal' I didn't expect. Can you explain what that covers?" },
  { key: 'new-roof-estimate', contact: :maple,     status: :open,     priority: :medium,
    source: 'Website',
    body: "Building a new detached garage (24x24) and need a roof on it. Can you do new construction, and how do I get an estimate?" },
  { key: 'emergency-storm',   contact: :henderson, status: :open,     priority: :urgent,
    source: 'Phone',
    body: "EMERGENCY — last night's storm tore shingles off and there's water coming into the upstairs bedroom. We need someone out today if possible. Please call ASAP." },
  { key: 'gutter-guards',     contact: :maple,     status: :resolved, priority: :low,
    source: 'Facebook',
    body: "Do you install gutter guards? If so what brands and roughly what does it run?" },
]

contacts = { henderson: henderson, maple: maple }

def status_int(status)
  Conversation.statuses[status.to_s]
end

created = 0
TICKETS.each do |t|
  contact = contacts[t[:contact]]
  ci = ContactInbox.find_by(inbox: inbox, source_id: t[:key])
  ci ||= ContactInbox.create!(inbox: inbox, contact: contact, source_id: t[:key])

  conv = Conversation.find_by(account: account, inbox: inbox, contact_inbox: ci)
  unless conv
    conv = Conversation.create!(
      account: account,
      inbox: inbox,
      contact: contact,
      contact_inbox: ci,
      additional_attributes: { 'source' => t[:source], 'ticket_key' => t[:key] }
    )
    # Seed the inbound customer message (this becomes the conversation subject/preview).
    Message.create!(
      account: account,
      inbox: inbox,
      conversation: conv,
      message_type: :incoming,
      content: t[:body],
      sender: contact
    )
    created += 1
  end

  # Normalize status + priority every run (idempotent).
  conv.update_columns(status: status_int(t[:status])) unless conv.status == t[:status].to_s
  conv.update!(priority: t[:priority]) unless conv.priority == t[:priority].to_s
  # Assign the emergency + warranty (high/urgent) to our agent so escalation reads true.
  if [:urgent, :high].include?(t[:priority]) && conv.assignee_id.nil?
    conv.update!(assignee: user)
  end
end

# --- 6. access token (the API credential the app.py uses) --------------------
# Chatwoot auto-creates an AccessToken for each User via a callback. Read it back.
token_rec = user.access_token || AccessToken.find_by(owner: user)
token_rec ||= AccessToken.create!(owner: user)
token = token_rec.token

counts = Conversation.where(account: account, inbox: inbox).group(:status).count
puts "SEED_OK account=#{account.id} inbox=#{inbox.id} contacts=#{Contact.where(account: account).count} " \
     "conversations=#{Conversation.where(account: account, inbox: inbox).count} " \
     "open=#{counts['open'] || 0} pending=#{counts['pending'] || 0} resolved=#{counts['resolved'] || 0} new=#{created}"
puts "ACCOUNT_ID=#{account.id}"
puts "ACCESS_TOKEN=#{token}"
