#!/bin/sh
# Generate the throwaway keypair the fake seedbox uses for pubkey-auth testing.
#
# The pair is NOT committed. A real OpenSSH private key in a repo trips secret scanners
# (GitHub push protection, gitleaks, trufflehog) and reads as a leak to anyone reviewing it,
# even when it is worthless — so it is generated on demand and gitignored instead.
#
# Run this before building the test seedbox; Dockerfile.gnu / Dockerfile.busybox COPY the
# .pub into authorized_keys at build time, so it must exist in the build context first:
#
#     docker/test-seedbox/gen_key.sh
#     docker compose -f docker-compose.test.yml up --build
#
# Password auth is also enabled on the fake seedbox, so the key path is only needed when
# exercising pubkey auth specifically.
set -eu

dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
key="$dir/test_key"

if [ -f "$key" ]; then
    echo "test_key already present — leaving it alone"
    exit 0
fi

ssh-keygen -t ed25519 -N '' -C 'lftpweb fake-seedbox throwaway key — not a secret' -f "$key"
chmod 600 "$key"
echo "generated $key (gitignored)"
