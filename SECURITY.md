# Security Policy

## Supported Versions

The project is pre-1.0. Security fixes are applied to the latest release and the
default branch. Older controller releases may require an upgrade before a fix can
be applied.

## Reporting A Vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue for a suspected vulnerability. Include affected versions, deployment
topology, reproduction steps, impact, and any suggested mitigation. Remove real
hostnames, usernames, repository URLs, SSH material, and workload output.

## Trust Model

Remote Runner intentionally executes operator-supplied shell commands and moves or
deletes remote artifacts during explicitly requested lifecycle operations. It
assumes:

- the local user, controller account, and project configuration are trusted;
- configured SSH host aliases resolve to the intended machines;
- compute worktrees and output roots are dedicated to the configured project;
- controller state directories are private to the controller account;
- workload commands are reviewed at the same trust level as local code execution.

It is not a security boundary between mutually hostile tenants. Process-title
privacy is best-effort metadata hygiene, not secret isolation.

`remote-runner dev` assumes the selected compute server and its administrator are
trusted. It filters common credential names, stores each source snapshot under a
private session directory, and uses marker-, ownership-, symlink-, and process-identity
guards for cleanup. This protects against accidental transfer/retention and unsafe path
deletion; it does not protect against server administrators, cloud providers, swap,
backups, forensic recovery, or malicious co-tenants. Session cleanup is not secure
erase, and the persistent dev cache may retain source-derived information.

Review every project `dev.include`, especially any rule that deliberately includes an
ordinary credential-like filename. Do not place secrets in source trees merely because
the default filter currently excludes their names. `dev` acquires no controller lease,
so avoid selecting a server whose durable workload must remain uncontended.

Use restricted forced-command SSH keys for output sources when possible. Keep
credentials in SSH agents or protected SSH configuration, never in project YAML.
Preview cleanup, purge, and prune operations before applying them, and retain
backups appropriate for the value of the workload output.
