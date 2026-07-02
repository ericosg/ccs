#!/bin/sh
# Launcher for ccs — runs the package from this repo's venv.
# Resolve symlinks (portable; BSD readlink has no -f) so `ccs` works when
# invoked via ~/.local/bin/ccs -> this file.
SELF="$0"
while [ -h "$SELF" ]; do
    LINK="$(readlink "$SELF")"
    case "$LINK" in
        /*) SELF="$LINK" ;;
        *)  SELF="$(dirname "$SELF")/$LINK" ;;
    esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
exec env PYTHONPATH="$DIR" "$DIR/.venv/bin/python" -m ccs "$@"
