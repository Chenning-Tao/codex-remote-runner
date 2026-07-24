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

Use restricted forced-command SSH keys for output sources when possible. Keep
credentials in SSH agents or protected SSH configuration, never in project YAML.
Preview cleanup, purge, and prune operations before applying them, and retain
backups appropriate for the value of the workload output.
