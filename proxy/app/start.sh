#!/bin/bash
set -euo pipefail

echo "==> Configuring SMTP proxy..."
python3 /app/configure.py

echo "==> Fixing permissions..."
chmod 644 /etc/postfix/master.cf /etc/postfix/main.cf 2>/dev/null || true

echo "==> Postfix config check..."
postfix check || { echo "ERROR: postfix check failed — see above"; exit 1; }

echo "==> Starting Dovecot (SASL backend)..."
/usr/sbin/dovecot

echo "==> Waiting for Dovecot auth socket..."
for i in $(seq 1 30); do
    if [ -S /var/spool/postfix/private/auth ]; then
        echo "    Auth socket ready after ${i}x0.5s"
        break
    fi
    sleep 0.5
done
if [ ! -S /var/spool/postfix/private/auth ]; then
    echo "ERROR: Dovecot auth socket missing at /var/spool/postfix/private/auth"
    echo "Dovecot status:"
    /usr/sbin/dovecot -F 2>&1 | head -20 || true
    exit 1
fi

echo "==> Starting Postfix in foreground..."
trap "postfix stop 2>/dev/null; /usr/sbin/dovecot stop 2>/dev/null; exit 0" SIGTERM SIGINT

# postfix start-fg (Postfix 3.4+) keeps master in foreground
exec postfix start-fg
