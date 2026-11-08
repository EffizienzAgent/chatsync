# Beeper Server (headless Beeper Client API) for chatsync. See docs/docker.md.
# Look up version and sha512 for your architecture (x64 or arm64) in
# https://api.beeper.com/desktop/update-feed.json?bundleID=com.automattic.beeper.server&platform=linux&channel=stable&arch=x64
# and pass them as build args; change version and checksum together.
ARG BEEPER_SERVER_VERSION=4.3.284
ARG BEEPER_SERVER_ARCH=x64
ARG BEEPER_SERVER_SHA512=6wR1h4W9mnuMxC28FckeQOGMzXMePblzbypCxcMIKmcgyJ50pEmIeYHehb4epB0Tfzyd630neeVcGYAIDIepCA==

FROM debian:12-slim
ARG BEEPER_SERVER_VERSION
ARG BEEPER_SERVER_ARCH
ARG BEEPER_SERVER_SHA512

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# The feed publishes the sha512 base64-encoded; the build fails on any mismatch.
RUN curl -fsSLo /tmp/server.tgz "https://beeper-desktop.download.beeper.com/builds/beeper-server-${BEEPER_SERVER_VERSION}-linux-${BEEPER_SERVER_ARCH}.tar.gz" \
    && echo "${BEEPER_SERVER_SHA512}" | base64 -d | od -An -tx1 -v | tr -d ' \n' > /tmp/expected \
    && sha512sum /tmp/server.tgz | cut -d' ' -f1 | tr -d '\n' > /tmp/actual \
    && cmp -s /tmp/expected /tmp/actual || { echo "sha512 mismatch for beeper-server ${BEEPER_SERVER_VERSION}"; exit 1; } \
    && mkdir -p /opt/beeper-server \
    && tar -xzf /tmp/server.tgz -C /opt/beeper-server --strip-components=1 \
    && rm /tmp/server.tgz /tmp/expected /tmp/actual

RUN groupadd -g 10001 beeper && useradd -u 10001 -g beeper -m -d /home/beeper beeper \
    && mkdir -p /data && chown beeper:beeper /data

USER beeper
ENV HOME=/home/beeper

EXPOSE 23373
VOLUME /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/beeper-server/beeper-server", "--host=0.0.0.0", "--port=23373", "--data-dir=/data/server"]
