#!/bin/sh
if ! /usr/bin/curl --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8787/api/health >/dev/null
then
  /usr/bin/systemctl --user restart textview-user.service
  exit 1
fi
