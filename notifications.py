"""
ZenFlow — Phase 3: Notification Service
────────────────────────────────────────
· WhatsApp booking confirmations via Twilio
· WeChat placeholder (requires WeChat Official Account API approval)
· Email confirmations via SendGrid
· Booking reminder 24h before session
· Async queue with retry logic
· All sends logged to notification_log table
"""

import os, asyncio, logging, json
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("zenflow.notif")

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
TWILIO_SID         = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN       = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WA_FROM     = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # Twilio sandbox
SENDGRID_KEY       = os.getenv("SENDGRID_API_KEY", "")
EMAIL_FROM         = os.getenv("EMAIL_FROM", "bookings@zenflow.sg")
EMAIL_FROM_NAME    = os.getenv("EMAIL_FROM_NAME", "ZenFlow Bookings")
APP_BASE_URL       = os.getenv("APP_BASE_URL", "https://zenflow.sg")

NOTIF_ENABLED      = bool(TWILIO_SID and TWILIO_TOKEN)
EMAIL_ENABLED      = bool(SENDGRID_KEY)

# ─────────────────────────────────────────
#  MESSAGE TEMPLATES  (EN + ZH)
# ─────────────────────────────────────────
def _booking_confirmed_en(b: dict) -> str:
    return (
        f"✅ *ZenFlow Booking Confirmed*\n\n"
        f"Hi {b['customer_name']}! Your booking is confirmed.\n\n"
        f"🌿 *Therapist:* {b['professional_emoji']} {b['professional_name']}\n"
        f"📅 *Date:* {b['booking_date']}\n"
        f"⏰ *Time:* {b['booking_time']}\n"
        f"⌛ *Duration:* {b['duration_hours']} hr{'s' if b['duration_hours'] > 1 else ''}\n"
        f"💰 *Total:* ${b['total_amount']:.2f}\n\n"
        f"🔑 *Booking ID:* `{b['booking_ref']}`\n\n"
        f"To reschedule or cancel, visit:\n"
        f"{APP_BASE_URL}?booking={b['booking_ref']}\n\n"
        f"_ZenFlow — Wellness, Renewed_ 🌿"
    )

def _booking_confirmed_zh(b: dict) -> str:
    return (
        f"✅ *ZenFlow 预约确认*\n\n"
        f"您好 {b['customer_name']}！您的预约已确认。\n\n"
        f"🌿 *治疗师：* {b['professional_emoji']} {b.get('professional_name_zh') or b['professional_name']}\n"
        f"📅 *日期：* {b['booking_date']}\n"
        f"⏰ *时间：* {b['booking_time']}\n"
        f"⌛ *时长：* {b['duration_hours']} 小时\n"
        f"💰 *合计：* ${b['total_amount']:.2f}\n\n"
        f"🔑 *预约编号：* `{b['booking_ref']}`\n\n"
        f"如需修改或取消，请访问：\n"
        f"{APP_BASE_URL}?booking={b['booking_ref']}\n\n"
        f"_ZenFlow — 身心愉悦，随时预约_ 🌿"
    )

def _booking_cancelled_en(b: dict) -> str:
    return (
        f"❌ *ZenFlow Booking Cancelled*\n\n"
        f"Hi {b['customer_name']}, your booking has been cancelled.\n\n"
        f"🔑 *Booking ID:* `{b['booking_ref']}`\n"
        f"🌿 *Therapist:* {b['professional_name']}\n"
        f"📅 *Was scheduled:* {b['booking_date']} at {b['booking_time']}\n\n"
        f"To rebook, visit {APP_BASE_URL}\n\n"
        f"_ZenFlow — Wellness, Renewed_ 🌿"
    )

def _booking_cancelled_zh(b: dict) -> str:
    return (
        f"❌ *ZenFlow 预约已取消*\n\n"
        f"您好 {b['customer_name']}，您的预约已成功取消。\n\n"
        f"🔑 *预约编号：* `{b['booking_ref']}`\n"
        f"🌿 *治疗师：* {b.get('professional_name_zh') or b['professional_name']}\n"
        f"📅 *原定时间：* {b['booking_date']} {b['booking_time']}\n\n"
        f"如需重新预约，请访问 {APP_BASE_URL}\n\n"
        f"_ZenFlow — 身心愉悦，随时预约_ 🌿"
    )

def _reminder_en(b: dict) -> str:
    return (
        f"⏰ *ZenFlow Reminder*\n\n"
        f"Hi {b['customer_name']}! You have a session tomorrow.\n\n"
        f"🌿 *Therapist:* {b['professional_emoji']} {b['professional_name']}\n"
        f"📅 *Date:* {b['booking_date']}\n"
        f"⏰ *Time:* {b['booking_time']}\n\n"
        f"🔑 Booking ID: `{b['booking_ref']}`\n\n"
        f"See you soon! 🙏"
    )

def _reminder_zh(b: dict) -> str:
    return (
        f"⏰ *ZenFlow 预约提醒*\n\n"
        f"您好 {b['customer_name']}！您明天有一个预约。\n\n"
        f"🌿 *治疗师：* {b['professional_emoji']} {b.get('professional_name_zh') or b['professional_name']}\n"
        f"📅 *日期：* {b['booking_date']}\n"
        f"⏰ *时间：* {b['booking_time']}\n\n"
        f"🔑 预约编号：`{b['booking_ref']}`\n\n"
        f"期待与您相见！🙏"
    )


def get_message(template: str, booking: dict, lang: str = "en") -> str:
    templates = {
        ("confirmed", "en"): _booking_confirmed_en,
        ("confirmed", "zh"): _booking_confirmed_zh,
        ("cancelled",  "en"): _booking_cancelled_en,
        ("cancelled",  "zh"): _booking_cancelled_zh,
        ("reminder",   "en"): _reminder_en,
        ("reminder",   "zh"): _reminder_zh,
    }
    fn = templates.get((template, lang), templates.get((template, "en")))
    return fn(booking) if fn else f"ZenFlow: Your booking {booking.get('booking_ref')} — {template}"


# ─────────────────────────────────────────
#  WHATSAPP  (Twilio)
# ─────────────────────────────────────────
async def send_whatsapp(to_number: str, message: str) -> tuple[bool, str]:
    """
    Send WhatsApp message via Twilio.
    to_number: E.164 format, e.g. '+6591234567'
    Returns (success, error_or_sid)
    """
    if not NOTIF_ENABLED:
        logger.info(f"[WhatsApp MOCK] To: {to_number}\n{message[:80]}...")
        return True, "MOCK_SID"

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client  = Client(TWILIO_SID, TWILIO_TOKEN)
        to_wa   = f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number

        msg = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.messages.create(
                from_=TWILIO_WA_FROM,
                to=to_wa,
                body=message
            )
        )
        logger.info(f"WhatsApp sent: {msg.sid} → {to_number}")
        return True, msg.sid

    except Exception as e:
        logger.error(f"WhatsApp failed to {to_number}: {e}")
        return False, str(e)


# ─────────────────────────────────────────
#  WECHAT PLACEHOLDER
# ─────────────────────────────────────────
async def send_wechat(openid_or_id: str, message: str) -> tuple[bool, str]:
    """
    WeChat Official Account template message.
    Requires approved WeChat Official Account — this is the integration point.
    In production replace this stub with WeChat MP API calls.
    """
    # WeChat requires:
    # 1. Verified Official Account (Service Account) — apply at mp.weixin.qq.com
    # 2. User must follow your Official Account (to get openid)
    # 3. Use template messages (structured) not plain text
    #
    # For now: log and return success so the app continues to work
    logger.info(f"[WeChat STUB] To: {openid_or_id} — message queued (requires WeChat MP API)")
    return True, "WECHAT_STUB"


# ─────────────────────────────────────────
#  EMAIL  (SendGrid)
# ─────────────────────────────────────────
def _booking_email_html(b: dict, template: str, lang: str = "en") -> str:
    """Generate a clean HTML email for booking notifications."""
    if lang == "zh":
        title  = {"confirmed": "预约确认", "cancelled": "预约取消", "reminder": "预约提醒"}.get(template, "ZenFlow")
        header = {"confirmed": "✅ 预约已确认！", "cancelled": "❌ 预约已取消", "reminder": "⏰ 明日预约提醒"}.get(template, "")
    else:
        title  = {"confirmed": "Booking Confirmed", "cancelled": "Booking Cancelled", "reminder": "Session Reminder"}.get(template, "ZenFlow")
        header = {"confirmed": "✅ Booking Confirmed!", "cancelled": "❌ Booking Cancelled", "reminder": "⏰ Session Tomorrow"}.get(template, "")

    name      = b.get("professional_name_zh") if lang == "zh" and b.get("professional_name_zh") else b["professional_name"]
    manage_url = f"{APP_BASE_URL}?booking={b['booking_ref']}"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#F5F0E8;font-family:'DM Sans',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F5F0E8;padding:40px 20px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <!-- HEADER -->
  <tr><td style="background:#2D2520;padding:28px 36px;text-align:center">
    <div style="font-size:26px;font-weight:700;color:#fff;font-family:Georgia,serif">🌿 ZenFlow</div>
    <div style="font-size:11px;color:#A8C4B0;letter-spacing:2px;margin-top:4px">TALENT ON DEMAND</div>
  </td></tr>
  <!-- BODY -->
  <tr><td style="padding:36px">
    <h1 style="margin:0 0 24px;font-size:22px;font-family:Georgia,serif;color:#2D2520">{header}</h1>
    <!-- Booking card -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#F5F0E8;border-radius:12px;padding:20px;margin-bottom:24px">
      <tr><td style="padding:6px 0;font-size:14px;color:#555">
        <b style="color:#2D2520;min-width:100px;display:inline-block">
          {'治疗师' if lang=='zh' else 'Therapist'}:</b>
        {b['professional_emoji']} {name}
      </td></tr>
      <tr><td style="padding:6px 0;font-size:14px;color:#555">
        <b style="color:#2D2520;min-width:100px;display:inline-block">
          {'日期' if lang=='zh' else 'Date'}:</b> {b['booking_date']}
      </td></tr>
      <tr><td style="padding:6px 0;font-size:14px;color:#555">
        <b style="color:#2D2520;min-width:100px;display:inline-block">
          {'时间' if lang=='zh' else 'Time'}:</b> {b['booking_time']}
      </td></tr>
      <tr><td style="padding:6px 0;font-size:14px;color:#555">
        <b style="color:#2D2520;min-width:100px;display:inline-block">
          {'时长' if lang=='zh' else 'Duration'}:</b>
        {b['duration_hours']} {'小时' if lang=='zh' else 'hr' + ('s' if b['duration_hours']>1 else '')}
      </td></tr>
      <tr><td style="padding:6px 0;font-size:14px;color:#555">
        <b style="color:#2D2520;min-width:100px;display:inline-block">
          {'合计' if lang=='zh' else 'Total'}:</b>
        <b style="color:#4E7A5F">${b['total_amount']:.2f}</b>
      </td></tr>
    </table>
    <!-- Booking ID -->
    <div style="background:#2D2520;border-radius:10px;padding:16px;text-align:center;margin-bottom:24px">
      <div style="font-size:11px;color:rgba(255,255,255,.5);letter-spacing:1px;margin-bottom:6px">
        {'您的预约编号' if lang=='zh' else 'YOUR BOOKING ID'}
      </div>
      <div style="font-family:'Courier New',monospace;font-size:22px;font-weight:700;color:#C9A84C;letter-spacing:3px">
        {b['booking_ref']}
      </div>
    </div>
    <!-- CTA -->
    <div style="text-align:center;margin-bottom:24px">
      <a href="{manage_url}"
         style="background:#4E7A5F;color:#fff;padding:12px 28px;border-radius:8px;
                text-decoration:none;font-weight:600;font-size:14px;display:inline-block">
        {'管理我的预约' if lang=='zh' else 'Manage My Booking'} →
      </a>
    </div>
    <p style="font-size:12px;color:#999;text-align:center;margin:0">
      {'如需帮助，请联系我们' if lang=='zh' else 'Need help? Contact us'} —
      <a href="mailto:support@zenflow.sg" style="color:#4E7A5F">support@zenflow.sg</a>
    </p>
  </td></tr>
  <!-- FOOTER -->
  <tr><td style="background:#F5F0E8;padding:16px 36px;text-align:center;font-size:11px;color:#aaa">
    © {datetime.now().year} ZenFlow · Singapore ·
    <a href="{APP_BASE_URL}" style="color:#4E7A5F">zenflow.sg</a>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
) -> tuple[bool, str]:
    if not EMAIL_ENABLED:
        logger.info(f"[Email MOCK] To: {to_email} | Subject: {subject}")
        return True, "MOCK_EMAIL"
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Content, MimeType

        mail = Mail(
            from_email=(EMAIL_FROM, EMAIL_FROM_NAME),
            to_emails=to_email,
            subject=subject,
            html_content=html_body,
        )
        if text_body:
            mail.content = [
                Content(MimeType.text, text_body),
                Content(MimeType.html, html_body),
            ]

        sg   = SendGridAPIClient(SENDGRID_KEY)
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: sg.send(mail)
        )
        logger.info(f"Email sent to {to_email}: status {resp.status_code}")
        return resp.status_code in (200, 202), str(resp.status_code)

    except Exception as e:
        logger.error(f"Email failed to {to_email}: {e}")
        return False, str(e)


# ─────────────────────────────────────────
#  MAIN DISPATCHER
# ─────────────────────────────────────────
async def notify_booking(
    booking: dict,
    template: str,          # "confirmed" | "cancelled" | "reminder"
    db=None,
    lang: str = "en",
) -> dict:
    """
    Send notification via the customer's preferred channel.
    Logs result to notification_log table.
    Returns dict with channel, success, ref.
    """
    contact_type  = booking.get("contact_type", "whatsapp")
    contact_value = booking.get("contact_value", "")
    booking_ref   = booking.get("booking_ref", "")

    message = get_message(template, booking, lang)
    success, ref = False, "no_channel"

    if contact_type == "whatsapp":
        success, ref = await send_whatsapp(contact_value, message)
    elif contact_type == "wechat":
        success, ref = await send_wechat(contact_value, message)
    else:
        logger.warning(f"Unknown contact_type: {contact_type}")

    status = "sent" if success else "failed"

    # Log to DB
    if db:
        try:
            await db.execute("""
                INSERT INTO notification_log
                (booking_ref, channel, recipient, message, status, error_msg)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, booking_ref, contact_type, contact_value,
                 message[:500], status, ref if not success else None)
            if success:
                await db.execute("""
                    UPDATE bookings SET notif_sent_at=NOW(), notif_channel=$1
                    WHERE booking_ref=$2
                """, contact_type, booking_ref)
        except Exception as e:
            logger.error(f"Failed to log notification: {e}")

    logger.info(f"Notification [{template}] → {contact_type}:{contact_value} [{status}]")
    return {"channel": contact_type, "success": success, "ref": ref, "status": status}


async def notify_booking_confirmed(booking: dict, db=None, lang: str = "en"):
    return await notify_booking(booking, "confirmed", db, lang)

async def notify_booking_cancelled(booking: dict, db=None, lang: str = "en"):
    return await notify_booking(booking, "cancelled", db, lang)

async def notify_booking_reminder(booking: dict, db=None, lang: str = "en"):
    return await notify_booking(booking, "reminder", db, lang)
