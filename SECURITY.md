# Security reports

Please report vulnerabilities privately through
[GitHub's security reporting page](https://github.com/Zethrus/Murmur/security/advisories/new).
If private reporting is unavailable, open an issue asking for a private contact
without including exploit details, credentials, or personal data.

Include the affected version, operating system, steps to reproduce, and expected
impact. Never attach your full app data directory: it may contain transcripts,
companion tokens, and a TLS private key.

Security fixes target the latest release. Older versions may need to be upgraded.
A release checksum detects changed downloads; it is not a signing certificate.

The phone listener is experimental and disabled by default. Keep it disabled
unless you are testing the companion protocol. Do not expose it through a reverse
proxy, port forward, or public tunnel.
