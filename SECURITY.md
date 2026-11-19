# Security

icelens reads catalogs it does not control: names, values and server errors are treated as
hostile, generated SQL is held to one statement, and credentials are kept out of every message.
`CONTRIBUTING.md` says how.

## Reporting a vulnerability

Report it privately, not in a public issue: on GitHub, open the repository's **Security** tab
and choose **Report a vulnerability**. Include the icelens version (`icelens --version`), what
you did and what happened. If a trace helps, attach it to the private report only; it holds
table names, namespaces and hostnames.

Worth reporting, for example: a catalog name or value that reaches the terminal as markup or
control codes, SQL that `--sql` runs although it writes, a credential that appears in output, a
trace or an error, or a request to the local REST catalog (`--serve`) that reaches files outside
its sandbox.

## Supported versions

Only the latest release gets fixes.
