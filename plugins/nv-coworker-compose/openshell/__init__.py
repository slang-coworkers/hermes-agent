"""OSH-F64 OpenShell deployment assets for nv-coworker-compose.

Build/deploy assets (not a separate plugin): the worker-sandbox ``Dockerfile.worker``,
the vendored ``dockerfile_lint`` checker, the pure state-aware ``installer`` planner, and
the ``install-into-sandbox.sh`` operator entrypoint. The ``install-openshell`` subaction
on the already-registered ``coworker`` CLI command loads ``installer`` from here.
"""
