# shellcheck shell=sh
# Replaces the base image's file. /etc/profile resets PATH in login shells, and the
# agent's terminal tool uses login shells, so the wrappers must come first here too.
PATH="/opt/hermes-app/bin:/opt/hermes/bin:/opt/hermes/.venv/bin:$PATH"
export PATH
