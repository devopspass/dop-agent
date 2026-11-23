# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-23

First public release.

### Added
- Always-on bridge between Slack, Telegram and a local Web UI and GitHub Copilot CLI (autopilot).
- Persistent multi-channel sessions with crash recovery, session groups, pins and titles.
- Web UI: sessions, inbox, `ask`/`actions`/`ideas` widgets, Mermaid diagrams, usage widget, Settings.
- Watchers (time, session turn, script and integration-provided) with token-saving script watches.
- Per-session model routing, voice transcription (Whisper), table and Markdown rendering.
- In-process MCP server at `/mcp`.
- Optional integration drivers (GitHub, GitLab, Bitbucket, Jenkins, Argo CD, Kargo, Artifactory, Jira, JSM,
  Confluence, ServiceNow, Grafana, Prometheus, Datadog, Splunk, PagerDuty/Opsgenie, Wiz, Terraform, Vault, Slack).
- EXPERIMENTAL Microsoft CDP drivers: Teams, Outlook, SharePoint/OneDrive, Power BI.
- Bundled skills, macOS launchd, Linux systemd and Docker deployment, Apache-2.0 license.

[Unreleased]: https://github.com/devopspass/dop-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/devopspass/dop-agent/releases/tag/v0.1.0
