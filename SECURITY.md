# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 2.5.x   | :white_check_mark: |
| < 2.5   | :x:                |

Only the latest release line receives security fixes. If you are running an older
version, please update via the standard update procedure (`git pull` + `install.sh`,
or `docker compose pull`).

## Reporting a Vulnerability

Please report security vulnerabilities through **GitHub Private Vulnerability
Reporting**: use the "Report a vulnerability" button under the repository's
Security tab, or go directly to
<https://github.com/maxg10/meshpulse/security/advisories/new>.

**Please do NOT open public issues for security problems.**

Response targets (best effort — MeshPulse is a spare-time open source project
maintained by one person):

- Acknowledgment within **7 days**.
- Fix timeline depends on severity: critical issues are prioritized; lower-severity
  issues are addressed in the next regular release.

### Scope

MeshPulse is self-hosted software. Reports about the configuration of your own
instance (e.g. exposing the web UI to the internet without protection) are **out of
scope** — see the design notes below. Vulnerabilities in MeshPulse code itself are
very much **in scope**, for example:

- XSS in the frontend (map, messages, stats, config pages)
- Injection in the backend or plugin system
- Path traversal in plugin installation
- Anything else that lets an attacker cross a trust boundary MeshPulse is supposed
  to enforce

## Security-relevant design notes

MeshPulse is designed to run on a **trusted LAN**: the web UI and the WebSocket
server (port 8765) have **no built-in authentication**, so they should not be
exposed to the internet without an authenticating reverse proxy or VPN. Plugin
packages execute arbitrary Python code with the privileges of the MeshPulse
service — install plugins only from trusted sources, such as the official store at
[meshpulse.app/plugins](https://meshpulse.app/plugins) or authors you trust.
