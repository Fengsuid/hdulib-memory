# Security

## Secrets

Never commit:

- `.env`,
- API keys,
- Cookie headers,
- saved user archives under `storage/`,
- deployment certificates or private keys.

Production deployments should set:

- `HDULIB_SIGNING_SECRET` to a long random value,
- `HDULIB_PUBLIC_BASE_URL` to the real HTTPS origin,
- a rate limiter or WAF in front of `/generate`, `/share-report`, and `/hakimi-review`.

## Built-In Protections

The web server includes:

- basic security response headers,
- report signatures for share and AI review endpoints,
- simple per-IP POST rate limits,
- POST body size limits,
- same-origin POST checks.

These are baseline protections, not a full replacement for a reverse proxy/WAF.

## Reporting Issues

If you discover a vulnerability, do not publish exploit details with real user data. Open a private issue or contact the maintainer directly.

