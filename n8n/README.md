# n8n Workflow Templates

Import these JSON files into your n8n instance to set up the automation layer.

## Setup

1. **Run n8n** locally or use n8n Cloud:
   ```bash
   docker run -it --rm -p 5678:5678 n8nio/n8n
   ```

2. **Configure credentials in n8n:**
   - HTTP Header Auth (for our API): Add `X-Webhook-Secret` = your WEBHOOK_SECRET
   - SMTP / SendGrid (for emails)
   - Lob API (for physical mail — optional)

3. **Import workflows:** In n8n, go to Workflows → Import from File

4. **Update URLs:** Replace `http://localhost:8000` with your deployed API URL

## Workflows

### 1. `dispute_ready_notification.json`
**Trigger:** Webhook receives `dispute-ready` event from our API
**Actions:**
- Fetch dispute summary (violations + letters) from our API
- Send email to user: "Your dispute analysis is ready"
- (Optional) Send Slack notification

### 2. `letter_mail_pipeline.json`
**Trigger:** Polls `/api/webhooks/pending-letters` every hour
**Actions:**
- Fetch approved letters
- Generate PDF via our `/api/letters/{id}/pdf` endpoint
- Send via Lob mail API (or download for manual sending)
- Mark letter as sent via `/api/webhooks/letter-mailed`

### 3. `thirty_day_tracker.json`
**Trigger:** Cron schedule — runs daily at 9 AM
**Actions:**
- Poll `/api/webhooks/overdue-disputes?days=30`
- For each overdue dispute: send reminder email to user
- If 45+ days: send escalation email with next-steps

### 4. `onboarding_flow.json`
**Trigger:** Webhook receives `user-signup` event
**Actions:**
- Send welcome email
- Wait 24 hours
- Send "Upload your first credit report" reminder
- Wait 72 hours
- If no report uploaded: send follow-up

### 5. `report_uploaded_flow.json`
**Trigger:** Webhook receives `report-uploaded` event
**Actions:**
- Send email: "Your {bureau} report has been parsed — {accounts_count} accounts found"
- If 3 reports uploaded: send "All bureaus uploaded — ready to analyze!"
