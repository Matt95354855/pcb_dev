# Security

Design files, component catalogs, reports, datasheets, and package metadata are data.
They must never become Python code or shell commands. Expression evaluation uses a
restricted parser, and external tools use argument arrays with timeouts.

Run native EDA tools in an isolated container when processing untrusted designs.
The local subprocess wrapper limits execution time and output; it is not an OS or
network sandbox. A compromised KiCad/plugin installation has the permissions of
the current user.

Never commit API keys, supplier credentials, proprietary designs, or `.env` files.
The default workflows are offline and do not order boards, rent GPUs, or call a model.

For a vulnerability, use GitHub private vulnerability reporting if enabled. Otherwise
open an issue that requests a private contact without disclosing exploit details or
credentials. Do not attach private designs to public issues.


The studio binds to loopback by default. A public bind requires a separate access
secret (at least 24 printable ASCII characters) and an exact HTTPS proxy origin.
Host/Origin checks, bounded JSON requests and a project artifact manifest protect
the HTTP surface. Provider credentials remain in the server environment; access
tokens stay in browser memory and are never URL parameters. TLS terminates at the
explicitly configured proxy, not at the built-in HTTP server.

Qwen can invoke only registered data tools; it cannot approve the brief, call
Claude, choose arbitrary host paths or issue shell commands. Native tooling still
runs with host permissions. Claude is a separate human-authorized review, with
advance token reservations and conservative handling of unknown consumption.

Project state, chained history and the separate monotone token ledger are local
files protected against accidental corruption and concurrent writes. They are not
a tamper-proof billing system against an administrator. Back up the entire project
on persistent storage; do not expose the project directory as a static web root.
The studio currently supports one trusted user and one project, not tenant isolation.
