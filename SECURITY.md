# Security Policy

We take security in KDIVE seriously and appreciate coordinated disclosure of
vulnerabilities.

## Reporting a vulnerability

Please do **not** open a public issue for a security vulnerability.

- **Preferred:** use GitHub private vulnerability reporting. Go to the
  repository's **Security** tab and choose **Report a vulnerability**. This opens
  a private advisory visible only to the maintainers.
- **Fallback:** if you cannot use the Security tab, email the maintainer at
  <randomparity@gmail.com>.

Include a description of the issue, the affected version, and steps to reproduce.
A minimal proof of concept helps us triage faster.

## Response expectations

- We aim to acknowledge a report within a few business days.
- We will work with you on a fix and a coordinated disclosure timeline, and will
  credit you in the advisory unless you prefer to remain anonymous.
- KDIVE does not operate a bug-bounty program; reports are handled on a
  best-effort, good-faith basis.

## Supported versions

KDIVE is pre-1.0 and follows SemVer in the `0.y.z` phase (see
[ADR-0041](docs/adr/0041-versioning-release-process.md)). Only the latest
released `0.y.z` receives security fixes. There is no long-term support for
older `0.y` lines; upgrade to the latest release to receive fixes.

| Version            | Supported          |
|--------------------|--------------------|
| Latest `0.y.z`     | :white_check_mark: |
| Any earlier release | :x:               |
