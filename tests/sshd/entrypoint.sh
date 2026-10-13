#!/bin/sh
set -eu
# Credentials come only from the test harness; production never uses this image.
: "${TEST_PASSWORD:?test password required}"
printf 'alice:%s\nbob:%s\nnopass:%s\nzerocache:%s\n' "$TEST_PASSWORD" "$TEST_PASSWORD" "$TEST_PASSWORD" "$TEST_PASSWORD" | chpasswd
unset TEST_PASSWORD
ssh-keygen -A
exec /usr/sbin/sshd -D -e -o PasswordAuthentication=yes -o PermitRootLogin=no -o LogLevel=ERROR
