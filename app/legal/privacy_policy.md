# Privacy Policy — {{app_name}}

**Effective date:** {{effective_date}}  
**Provided by:** {{company_name}} ("we", "us", "our")

This Privacy Policy explains what personal data the **{{app_name}}** mobile app and its services collect, why, how it is protected, who it is shared with, how long it is kept, and the choices and rights you have. It applies to the {{app_name}} Android app and the servers that support it.

By creating an account you agree to the processing described here. If you do not agree, please do not use the app.

## 1. Who we are and how to contact us

- **Data fiduciary / developer:** {{company_name}}
- **Address:** {{company_address}}
- **Privacy contact:** {{contact_email}}
- **Grievance Officer:** {{grievance_officer}} — {{grievance_email}}

You can contact us about anything in this policy, including requests to access, correct or delete your data.

## 2. The data we collect

We collect only what we need to run the app.

| Data | Required? | Why we collect it |
|---|---|---|
| **Mobile number** | Yes | Your account identity and to send you a one-time password (OTP) to sign in. |
| **Name** | Yes | To personalise your account. |
| **Email address** | Optional | To contact you about your account, if you choose to add it. |
| **State** (e.g. Maharashtra) | Optional | To understand where our users are and tailor content. |
| **One-time passwords (OTP)** | Yes, temporary | To verify that you own the mobile number. Stored only as a secure hash. |
| **Sign-in session data** | Yes | To keep you signed in on your device (securely hashed session tokens). |
| **Account activity dates** | Automatic | When you signed up, verified your number and last signed in — for security and support. |
| **IP address** | Automatic, temporary | To protect the service from abuse (limits on how often OTPs can be requested). |
| **Technical logs** | Automatic | Server logs (time, requested address, IP, result) to keep the service secure and working. |

**We do not collect:** your contacts, photos, files on your phone, precise location, payment or card details, health records, or advertising identifiers. We do not use your data for advertising and we do **not sell** your personal data.

## 3. How we use your data

- To create and secure your account and sign you in with OTP.
- To show you the videos and documents available in the app.
- To prevent fraud and abuse (for example, limiting repeated OTP requests) and to keep the service secure.
- To respond to your questions, requests and complaints.
- To meet legal obligations.

We process your data on the basis of your **consent**, given when you create your account, and for the legitimate uses permitted by law (such as security and legal compliance). You can withdraw consent at any time by deleting your account (see Section 8); this does not affect processing done before withdrawal.

## 4. Who we share data with

We share personal data only with service providers that help us run the app, only as needed, and under confidentiality and security obligations:

| Provider type | What they receive | Purpose |
|---|---|---|
| **SMS delivery provider** ({{sms_provider}}) | Your mobile number and the OTP message | To deliver your sign-in OTP. |
| **Database hosting** (MongoDB Atlas) | Your account data (Section 2) | Secure storage of the app's database. |
| **Server and file hosting** ({{hosting_provider}}) | Account data, logs; videos and documents we publish | To run the app's servers and deliver content. |

We may also disclose data if required by law, a court order or a government authority, or to protect the rights, safety and security of our users and the service. If our business is merged or transferred, your data may be transferred subject to this policy.

We do not share your data with advertisers or data brokers.

## 5. Videos and documents in the app

Videos and documents in the app are for **viewing only inside the app**. They are delivered through secure links that expire, are not offered for download, and documents are shown as page images rather than as the original file. The app may block screenshots and screen recording on these screens. We do not track which videos or pages you view.

## 6. How we protect your data

- All data is transferred over encrypted connections (HTTPS/TLS).
- OTPs and session tokens are stored only as secure one-way hashes, never in plain text; OTPs expire after a few minutes and allow limited attempts.
- The database is hosted on MongoDB Atlas, which encrypts data at rest.
- Access to administrative tools is restricted to authorised administrators with separate, short-lived admin sessions.
- Media links are signed and expire automatically.

No system is completely secure, but we take reasonable technical and organisational measures to protect your data. If a personal data breach affects you, we will inform you and the relevant authority as required by law.

## 7. How long we keep your data

| Data | Kept for |
|---|---|
| One-time passwords | Up to 5 minutes (deleted once used or expired) |
| Unfinished sign-ups (number never verified) | Automatically deleted after 24 hours |
| Abuse-protection counters (include IP address) | Up to 24 hours |
| Sign-in sessions | Up to 30 days, or until you sign out |
| Account data | While your account is active; deleted when you delete your account |
| Server logs | Up to {{log_retention_days}} days, unless needed to investigate a security incident |

When you delete your account, we delete your account data and sign you out of all devices within **{{deletion_days}} days**, except where we must keep limited information to comply with the law.

## 8. Your rights and choices

Under India's Digital Personal Data Protection Act, 2023 and applicable law, you can:

- **Access** a summary of the personal data we hold about you and how it is processed.
- **Correct and update** your name, email and state in the app's profile, or ask us to correct other data.
- **Delete** your account and personal data (erasure).
- **Withdraw consent** at any time by deleting your account.
- **Nominate** a person to exercise your rights in case of death or incapacity.
- **Raise a grievance** with our Grievance Officer (Section 1). We will respond within **30 days**.
- If you are not satisfied, **complain to the Data Protection Board of India**.

**How to delete your account:** in the app, or by visiting {{deletion_url}} and following the instructions there, or by emailing {{contact_email}} with your registered mobile number. We will verify that the request comes from the account owner before deleting.

## 9. Children

{{app_name}} is intended for users aged **18 and above**. We do not knowingly collect personal data from children under 18. If you believe a child has created an account, contact us and we will delete it.

## 10. Changes to this policy

We may update this policy from time to time. The latest version is always available at {{policy_url}}, with its effective date at the top. If we make significant changes, we will notify you in the app before they take effect.

## 11. Governing law

This policy is governed by the laws of India, including the Information Technology Act, 2000 and the Digital Personal Data Protection Act, 2023, and the rules made under them.

---

Questions? Contact us at **{{contact_email}}**.
