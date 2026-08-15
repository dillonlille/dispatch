# Authentication

Status: **credential storage and bounded provider login workflow implemented; live-account acceptance remains pending**.

Authentication is a Dispatch Core feature, not a plugin. It owns fixed Amazon and Paycom authentication realms, private credential enrollment, bounded status, credential removal, trusted in-process credential access, and canonical landing-page verification.

Credentials are encrypted at rest in a user-owned `0700` directory under the resolved Core configuration root. The key, encrypted vault, and process lock are `0600`. Enrollment accepts secret values only through the trusted Python API or hidden terminal prompts; status and command output never include credential values. This per-user encryption protects stored material from accidental disclosure and other OS users, but it is not an OS sandbox against untrusted code running as the same user.

Supported realms:

- `amazon-operations` → `https://logistics.amazon.com/dspconsolev2`
- `paycom-client` → `https://www.paycomonline.net/v4/cl/web.php/client-landing/arc`

The workflow navigates only the realm's canonical landing URL and recognizes only fixed Amazon and Paycom login authorities and fields. It fills enrolled usernames and passwords, answers exactly two unambiguous Paycom security-PIN fields from the five configured PINs, and returns bounded statuses. MFA and CAPTCHA are never automated: the workflow returns `mfa_required` or `captcha_required`, preserves the Browser Manager session for the user, and continues only through an explicit `resume` call. A successful flow returns to and verifies the canonical landing page.

Unexpected hosts, ports, forms, challenge counts, or page states fail closed as `manual_verification_required` or `auth_unavailable`. Raw credentials, Playwright pages, cookies, and profile paths never appear in `AuthenticationResult.safe_data()` or command output. Login selectors are Core-owned authentication details; domain collection selectors remain outside Core.

The public pages and fixed login fields have been inspected, and the workflow is covered with simulated Browser Manager sessions. Authorized live Amazon and Paycom acceptance has not yet been performed, so the Core manifest remains `in-progress`.
