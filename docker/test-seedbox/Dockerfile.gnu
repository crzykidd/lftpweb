# Fake seedbox #1: GNU findutils present, so `find -printf` (DESIGN.md §5's primary scan
# path) works. Debian slim is the simplest way to get a real GNU find + real sshd for a
# throwaway, localhost-only integration fixture (DESIGN.md §14) — this image is never
# published, only built and run by docker-compose.test.yml.

FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server python3 findutils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

# Test-only throwaway credentials (see sshd_config's comment) — both password and key auth
# are enabled so lftpweb's HostConfig.auth_method 'password' and 'key' paths can each be
# exercised against a real seedbox.
RUN useradd -m -s /bin/bash seeduser \
    && echo 'seeduser:testpass123' | chpasswd \
    && mkdir -p /home/seeduser/.ssh \
    && chmod 700 /home/seeduser/.ssh

COPY test_key.pub /home/seeduser/.ssh/authorized_keys
COPY sshd_config /etc/ssh/sshd_config
COPY seed_tree.sh /usr/local/bin/seed_tree.sh

RUN chmod +x /usr/local/bin/seed_tree.sh \
    && chmod 600 /home/seeduser/.ssh/authorized_keys \
    && /usr/local/bin/seed_tree.sh /data/pickup \
    && chown -R seeduser:seeduser /home/seeduser /data

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
