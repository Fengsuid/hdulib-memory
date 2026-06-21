# Privacy

This project is a non-official memory report generator. It processes library booking history only when a user actively imports data.

## What May Be Stored

If saving is enabled, the server stores:

- imported booking history JSON,
- generated report data,
- a password hash for the local save passphrase,
- a public report copy if the user shares the report.

The project should not store unified authentication passwords.

## Cookie Import

Cookie import is a fallback only. It can grant temporary access to the user's library session and should be treated as sensitive. Prefer JSON import when possible.

## Public Reports

Shared reports may expose student ID, usage statistics, generated tags, and AI commentary. Deployments should clearly explain this before users share a report.

## Recommended Deployment Policy

- Provide a way for users to request deletion.
- Do not publish raw `storage/` files.
- Do not log Cookie headers or raw imported JSON.
- Use HTTPS in production.

