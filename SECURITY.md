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
