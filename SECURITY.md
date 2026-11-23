# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/devopspass/dop-agent/security/advisories/new)
(repository → Security → Report a vulnerability). Include affected version, reproduction steps and
impact. Expect an acknowledgement within a few days; fixes are released as patch versions and credited
in the advisory unless you prefer otherwise.

## Threat model summary

DOP Agent is a **single-user, locally run** tool that gives a GitHub Copilot agent (autopilot,
`approve_all`) the full privileges of the user account it runs under.

| Area | Assumption / control |
|---|---|
| Trust boundary | Everyone who can message the agent is trusted as the owner. Chat access is allow-listed (Slack/Telegram user ids; empty list denies all). |
| Web UI | Binds to loopback; shared password required; per-IP login rate limit; CSRF header + same-origin checks; strict CSP; sanitized Markdown. Not hardened for LAN/internet exposure. |
| Prompt injection | Text from tickets, PRs, chats, web pages and files is untrusted and can steer the agent. Mitigate with least-privilege tokens, a dedicated user or Docker, and confirm-first write commands in drivers. |
| Secrets | Referenced via `${env:}`/`${file:}`/`${keychain:}`; config and state files in `~/.dop-agent` are `0600`; secrets are not printed by drivers. |
| Data at rest | Transcripts and state are stored unencrypted in `~/.dop-agent`. |
| Network | Outbound only (GitHub Copilot, Slack Socket Mode, Telegram long polling, configured integrations). No telemetry. |
| Experimental drivers | Microsoft CDP drivers use a signed-in browser session and undocumented endpoints; see the README. |

## Out of scope

Anything that requires an already-authorized user to run commands through the agent (that is its purpose), and
issues in third-party services or in the GitHub Copilot CLI itself.
