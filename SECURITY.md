# Security Policy

## Reporting a Vulnerability

Please do not open a public GitHub issue for security vulnerabilities.

If you discover a vulnerability involving authentication, authorization,
credentials, infrastructure configuration, data integrity, or other security
concerns, please report it privately to the project maintainer.

Do not include active credentials, API keys, tokens, or other sensitive
information in public issues.

## Secrets

Hidden Conviction does not require secrets to be committed to source control.

Production credentials should be stored using Cloudflare Workers secrets or
equivalent secure environment configuration.

Never commit:

- ADMIN_SECRET
- Cloudflare API tokens
- private keys
- authentication tokens
- local `.dev.vars`
- database backups
