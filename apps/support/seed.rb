# Idempotent seed for the Meridian Wealth Management demo tenant on self-hosted Chatwoot.
# Run via:  rails runner /tmp/meridian_support_seed.rb   (inside the chatwoot rails container)
#
# Creates / updates (all by stable natural keys, safe to re-run):
#   * a super-admin User (the agent),
#   * the Account "Meridian Wealth Management",
#   * an API channel inbox,
#   * 2 contacts,
#   * ~7 client-service conversations across open / pending / resolved,
#   * an inbound message on each so the queue/feed has real subjects.
#
# Prints on success:
#   SEED_OK account=<id> inbox=<id> contacts=<n> conversations=<n> open=<n> ...
#   ACCOUNT_ID=<id>
#   ACCESS_TOKEN=<user.access_token.token>

ADMIN_EMAIL    = 'admin@meridianwealth.com'
ADMIN_NAME     = 'Meridian Wealth Support Agent'
ADMIN_PASSWORD = 'MeridianWealth!2026'
ACCOUNT_NAME   = 'Meridian Wealth Management'
INBOX_NAME     = 'Meridian Wealth Support'

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

henderson = upsert_contact(account, 'Whitfield Family Trust', 'whitfield.trust@example.com', '+15125550142')
maple     = upsert_contact(account, 'Okonkwo Holdings',       'okonkwo.holdings@example.com', '+15125550199')

# --- 5. conversations (idempotent by a deterministic identifier) -------------
# Each entry: contact, status, priority, channel-ish source label, inbound text.
TICKETS = [
  { key: 'statement-access',  contact: :henderson, status: :open,     priority: :medium,
    source: 'Website',
    body: "Hi — I can't log in to the client portal to download my latest quarterly statement. Can you help me regain access to the Whitfield Family Trust account, or email the statement over?" },
  { key: 'acat-transfer-status', contact: :maple,  status: :open,     priority: :low,
    source: 'Phone',
    body: "I started moving my old brokerage account over to you a couple of weeks ago. Can you tell me the status of the ACAT transfer for Okonkwo Holdings?" },
  { key: 'beneficiary-update', contact: :henderson, status: :pending, priority: :high,
    source: 'Email',
    body: "We need to update the beneficiary designations on the Whitfield Family Trust accounts after a change in the family. What's the process, and can someone walk us through it?" },
  { key: 'advisory-fee-question', contact: :maple, status: :pending,  priority: :medium,
    source: 'Email',
    body: "Quick question on the last fee invoice — there's an advisory-fee line item that's higher than last quarter. Can you explain how the advisory fee is calculated?" },
  { key: 'performance-report-request', contact: :maple, status: :open, priority: :medium,
    source: 'Website',
    body: "Could you send a year-to-date performance report for the Okonkwo Holdings portfolio? I'd like to review returns before our next meeting." },
  { key: 'account-access-locked', contact: :henderson, status: :open, priority: :urgent,
    source: 'Phone',
    body: "URGENT — I got a login alert I don't recognize and now I'm locked out of my account. Please call ASAP so we can secure the Whitfield Family Trust accounts." },
  { key: 'tax-document-timing', contact: :maple,    status: :resolved, priority: :low,
    source: 'Facebook',
    body: "When will my 1099 and other tax documents be available for last year? My accountant is asking." },
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
  # Assign the urgent access + beneficiary-update (high/urgent) to our agent so escalation reads true.
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
