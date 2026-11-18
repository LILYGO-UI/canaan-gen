FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash binutils binfmt-support ca-certificates coreutils debootstrap debian-archive-keyring \
    e2fsprogs file git gpg kmod python3 qemu-user-static qemu-user-binfmt rsync util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . /src/
ENTRYPOINT ["/bin/bash", "/src/scripts/docker-entrypoint.sh"]
