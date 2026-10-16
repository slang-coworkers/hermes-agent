# Sourced by login shells (/etc/profile). The github provider places its session credential
# under its own name, which the GitHub CLI does not read; expose it under the CLI's names.
if [ -n "${api_token:-}" ]; then
  if [ -z "${GH_TOKEN+x}" ]; then
    export GH_TOKEN="${api_token}"
  fi
  if [ -z "${GITHUB_TOKEN+x}" ]; then
    export GITHUB_TOKEN="${api_token}"
  fi
fi
