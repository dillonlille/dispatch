# Authentication profiles

Status: **profile enrollment and bounded provider login workflow implemented; live-account acceptance remains pending**.

Authentication is a Dispatch Core feature, not a plugin. Operators work with globally unique lowercase profile names. A profile has one Core-owned provider, encrypted credentials, and an explicit `enrolled`/`unverified` state until a future explicit authentication check is run. Setup enrollment does not browse or prove a live login.

## Public commands

```bash
dispatch auth list
dispatch auth add amazon-work --provider amazon
dispatch auth status amazon-work
dispatch auth remove amazon-work --yes
```

`add` reads profile-type fields through hidden terminal prompts. Secret values are never accepted as arguments, JSON, environment values, logs, receipts, or output. The public profile types are **Amazon Operations** (`amazon`) and **Paycom** (`paycom`); their URLs, login policy, and credential schemas remain internal Core authority. Profile names are immutable in v1. To rotate credentials, create a new named profile, select it during setup, then remove the unreferenced old profile; this also creates fresh plugin-isolated browser state.

Interactive `dispatch setup` asks for a compatible existing profile or a new profile after exact plugin selection succeeds. `--yes`, JSON, and other noninteractive setup paths never prompt for secrets; they return a safe `pending_requirements` result directing the operator back to interactive setup when an authenticated plugin has no selected enrolled profile.

The current install-validated plugin requirements are:

- Companion Bridge: one `amazon-operations` profile;
- Paycom: one `paycom-client` profile;
- Handbook: no profile.

A profile can be reused by multiple plugins that declare the same provider. Browser state remains plugin-isolated by the existing Core layout; sharing a credential profile never shares browser state.

## Storage and compatibility

Credentials remain in the encrypted Core vault under the private secrets root. A separate private, non-secret registry maps a profile name to its provider, internal vault alias, enrollment state, and selected plugin projection. Registry writes are bounded, owner-only, locked, atomic, and durable. Registry output contains no credential values.

Existing `(provider, account_alias)` vault records remain readable. On profile use, Core deterministically projects legacy records into named profiles without re-entering credentials. Conflicting or orphaned records remain private and are reported as incompatible/orphaned; Core never silently exposes or deletes their credentials. Legacy `auth enroll|status|remove` calls remain compatibility wrappers, but profile commands are the primary UX.

## Runtime boundary

Core resolves the profile selected during setup before service or collection work. Companion uses it through a plugin-scoped authentication broker. Paycom retains legacy explicit `--account` compatibility, while normal collection uses the profile selected during setup. Credentials never enter `CollectionContext` or JSON. Read-only plugin queries and health do not authenticate, browse, or collect. Enabling the Companion service requires its selected enrolled profile; selecting Paycom does not schedule or collect anything.

Core owns provider URLs, browser realms, login selectors, challenge handling, and the encrypted vault. Plugins declare only a required Core provider identifier in install-validated `pyproject.toml` metadata.
