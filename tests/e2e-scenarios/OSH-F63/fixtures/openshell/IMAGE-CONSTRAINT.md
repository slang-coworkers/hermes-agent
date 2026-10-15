# OpenShell sandbox `--from` image constraint (operator step 3, LANE READY 2026-09-23)

The pinned `--from` image the OSH-F63 provisioning plan uses
(`egress.sandbox_image` in the spec) is **`osh-f63-base:trixie`** — image id
`06f0220e7810`, Debian **trixie**, glibc **2.41**.

Why the trixie / glibc ≥ 2.39 base: OpenShell injects PID 1
`/opt/openshell/bin/openshell-sandbox` into every sandbox, and that binary is linked
against glibc ≥ 2.39. On an alpine (musl) or Debian bookworm (glibc 2.36) base it
restart-loops and the sandbox never reaches Ready.

**The broker refuses any `--from` other than the exact `osh-f63-base:trixie`**, so
the tag is broker-pinned — it is NOT an operator-swappable placeholder and must not be
replaced with a different tag or digest. A maintainer changing the base image must
re-pin the broker's allowed `--from` and preserve the glibc/OS constraint, updating
both this note and `spec/openshell/coworker-types.yaml` (`egress.sandbox_image`).

This constraint is proven on-box by AC-OSH-F63-5 (an image that violates it fails
step 1: the sandbox never reaches Ready).
