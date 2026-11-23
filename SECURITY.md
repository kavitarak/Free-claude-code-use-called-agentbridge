# Security

AgentBridge handles API keys, so please report problems privately.

- Email **kavitarak@zohomail.in** with the subject "AgentBridge security", or use GitHub's "Report a vulnerability" button on this repo.
- Please include the version (`agentbridge --version`), your OS and steps to reproduce. Do not paste real API keys.
- I will reply as soon as I can and credit you in the fix unless you prefer not to be named.

## Design rules (please keep them when contributing)

1. Listen on localhost unless `AGENTBRIDGE_AUTH_TOKEN` is set.
2. Never log request or response bodies, or headers.
3. Run every error text that can reach a log or a client through `Upstream.scrub()`.
4. Keep the project dependency free, so there is little to audit.
