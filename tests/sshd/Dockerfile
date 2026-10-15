FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server sudo bash ca-certificates rsync && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd && useradd -m -s /bin/bash alice && useradd -m -s /bin/dash bob && useradd -m -s /bin/bash nopass && useradd -m -s /bin/bash zerocache
RUN printf 'alice ALL=(ALL) ALL\nbob ALL=(ALL) ALL\nnopass ALL=(ALL) NOPASSWD: ALL\nzerocache ALL=(ALL) ALL\nDefaults:zerocache timestamp_timeout=0\nDefaults lecture=never\n' > /etc/sudoers.d/fixture && chmod 440 /etc/sudoers.d/fixture
COPY entrypoint.sh /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
