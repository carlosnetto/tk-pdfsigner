#!/bin/sh
mkdir -p /run/pcscd
pcscd --foreground --disable-polkit >/tmp/pcscd.log 2>&1 &
sleep 3
exec "$@"
