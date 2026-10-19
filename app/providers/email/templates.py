"""
templates.py — The OTP email.

Kept deliberately bare: the code, how long it lasts, and a line telling the
user to ignore the message if they didn't ask for it. No links, no tracking
pixels, no images.

That is partly good taste and partly deliverability — a short, plain,
link-free message from an authenticated domain is the least likely thing to be
filtered. It is also security: whatever provider carries this mail can read it,
so it should contain nothing beyond the code itself.
"""

from app.core.config import settings


def otp_subject(otp: str) -> str:
    # Set EMAIL_OTP_SUBJECT in .env to change this without touching code.
    # "{otp}" and "{app}" are substituted if present.
    #
    # Note: putting the code in the subject lets users read it from the phone's
    # notification banner without opening the mail. The default below keeps the
    # code out of the subject, which is tidier but costs that convenience.
    return settings.EMAIL_OTP_SUBJECT.format(
        otp=otp, app=settings.APP_NAME_SHORT
    )


def otp_text(otp: str, purpose_label: str) -> str:
    return (
        f"{otp}\n\n"
        f"Use this code to {purpose_label}. "
        f"It expires in {settings.OTP_EXPIRE_MINUTES} minutes.\n\n"
        "If you didn't request this, you can ignore this email — "
        "someone may have typed your address by mistake.\n\n"
        "Never share this code with anyone. "
        f"{settings.APP_NAME_SHORT} staff will never ask you for it.\n"
    )


def otp_html(otp: str, purpose_label: str) -> str:
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f6f7f9;
               font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
               color:#1a1a1a;">
    <div style="max-width:420px;margin:0 auto;background:#ffffff;border-radius:12px;
                padding:32px;border:1px solid #e5e7eb;">
      <p style="margin:0 0 20px;font-size:15px;color:#4b5563;">
        Use this code to {purpose_label}.
      </p>
      <div style="font-size:34px;font-weight:700;letter-spacing:7px;
                  text-align:center;padding:18px 0;color:#111827;">
        {otp}
      </div>
      <p style="margin:20px 0 0;font-size:14px;color:#6b7280;">
        It expires in {settings.OTP_EXPIRE_MINUTES} minutes.
      </p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
      <p style="margin:0 0 10px;font-size:13px;color:#6b7280;">
        If you didn't request this, you can ignore this email — someone may have
        typed your address by mistake.
      </p>
      <p style="margin:0;font-size:13px;color:#6b7280;">
        Never share this code. {settings.APP_NAME_SHORT} staff will never ask you for it.
      </p>
    </div>
  </body>
</html>
"""


#: What the code is for, in a form that reads naturally mid-sentence.
PURPOSE_LABELS = {
    "app": "sign in",
    "admin": "sign in to the admin dashboard",
    "delete": "confirm deleting your account",
    "signup": "finish creating your account",
}


def label_for(purpose: str) -> str:
    return PURPOSE_LABELS.get(purpose, "verify your account")
