#!/bin/sh
# xbridge venv launcher (launchd-safe).
# launchd canonicalizes ProgramArguments to the CLT python, and its re-exec loses
# the venv (pyvenv.cfg is then not found from the framework dir) -> zeroconf/ifaddr
# silently missing. This wrapper makes the venv explicit via PYTHONPATH instead.
# Resolve the venv next to this script, so the repo can live anywhere.
DIR=$(cd "$(dirname "$0")" && pwd)
VENV="$DIR/.venv-xbridge"
VER="$("$VENV/bin/python3" -c 'import sys; print("python%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ -n "$VER" ] && [ -d "$VENV/lib/$VER/site-packages" ]; then
    PYTHONPATH="$VENV/lib/$VER/site-packages${PYTHONPATH:+:$PYTHONPATH}" exec /usr/bin/python3 "$@"
fi
exec "$VENV/bin/python3" "$@"   # fallback: manual use keeps normal venv semantics
