# OpenShell sandbox `--from` image constraint (operator step 3)

The pinned `--from` image the OSH-F63 provisioning plan uses
(`egress.sandbox_image` in the spec, currently
`localhost/hermes-openshell-sandbox:pinned`) **must be Debian trixie / Ubuntu 24.04
based — glibc ≥ 2.39.**

Why: OpenShell injects PID 1 `/opt/openshell/bin/openshell-sandbox` into every
sandbox, and that binary is linked against glibc ≥ 2.39. On an alpine (musl) or
Debian bookworm (glibc 2.36) base it restart-loops and the sandbox never reaches
Ready.

Until OSH-F64 ships the dedicated worker Dockerfile, use a community image on a
trixie / 24.04 base (route (a): nothing extra; route (b): ships `sshd`).

**The operator supplies the immutable `--from` digest at LANE READY** and replaces
the placeholder tag with it, keeping the base trixie / 24.04. A maintainer re-pinning
the image must preserve this glibc/OS constraint — record the new digest here and in
`spec/openshell/coworker-types.yaml` (`egress.sandbox_image`).

This constraint is proven on-box by AC-OSH-F63-5 (an image that violates it fails
step 1: the sandbox never reaches Ready).
