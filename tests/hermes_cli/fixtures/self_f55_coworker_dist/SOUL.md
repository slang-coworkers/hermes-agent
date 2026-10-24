# self-f55-coworker

Example coworker-type persona shipped as a Hermes profile distribution for the
SELF-F55 port. In the fleet, one coworker type is authored once as a distribution
like this and installed per machine with `hermes profile install`, so the type's
personality, skills, cron jobs, and config travel together while each installer
supplies its own credentials.

This SOUL.md is distribution-owned: `hermes profile update` re-applies it from the
source, while the installer's memories, sessions, and `.env` are never touched.
