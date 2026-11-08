# Running Beeper Server with Docker Compose

chatsync talks to a self-hosted [Beeper](https://www.beeper.com) Server through its Client API. This
guide adds one to an existing Frappe stack that runs with Docker Compose, for example
[frappe_docker](https://github.com/frappe/frappe_docker). Service names below (`backend`,
`queue-short`, `queue-long`) are the ones frappe_docker uses; adjust them to your stack.

What was checked: the image, the listen flags, the update feed, the CLI sign-in flow and the
resource numbers come from a working setup (Beeper Server 4.3.284, `beeper-cli` 0.6.2, linux/x64
and linux/arm64). Items marked **unverified** were not tried in exactly this form.

## 1. Build the image

The repository ships a Dockerfile in [`contrib/beeper-server`](../contrib/beeper-server/Dockerfile):
Debian slim with `ca-certificates curl tini`, the pinned Linux tarball, a sha512 check that fails
the build on a mismatch, a non-root user (uid 10001), `HOME` inside the image and all state under
`/data`.

Look up the current version and checksum for your architecture (`x64` or `arm64`):

```bash
curl -s "https://api.beeper.com/desktop/update-feed.json?bundleID=com.automattic.beeper.server&platform=linux&channel=stable&arch=x64"
# {"version":"4.3.284","url":"…/beeper-server-4.3.284-linux-x64.tar.gz","sha512":"6wR1…==", …}
```

Build with these values (version, arch and checksum always change together):

```bash
docker build \
  --build-arg BEEPER_SERVER_VERSION=4.3.284 \
  --build-arg BEEPER_SERVER_ARCH=x64 \
  --build-arg BEEPER_SERVER_SHA512='6wR1h4W9mnuMxC28FckeQOGMzXMePblzbypCxcMIKmcgyJ50pEmIeYHehb4epB0Tfzyd630neeVcGYAIDIepCA==' \
  -t beeper-server:4.3.284 contrib/beeper-server
```

The server starts with `beeper-server --host=0.0.0.0 --port=23373 --data-dir=/data/server`. Keep
port 23373: the server advertises its OAuth endpoints with the port it listens on, so container
port and the port clients use must be the same.

## 2. Add the service

Add this to your `compose.yaml` (or an override file). Beeper sits on an internal network that only
the Frappe backend and workers join, plus an egress network of its own for the internet (it needs
outbound HTTPS to Beeper and the chat networks). Nothing publishes its port.

```yaml
services:
  beeper:
    image: beeper-server:4.3.284
    restart: unless-stopped
    networks: [beeper, beeper-egress]
    volumes:
      - beeper-data:/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:23373/v1/info"]
      interval: 30s
      timeout: 5s
      start_period: 60s
      retries: 3
    mem_reservation: 1g   # ~550 MiB idle
    mem_limit: 4g         # the first sync peaked at ~3 GiB
    # Unverified: a read-only root file system. The server writes to /data and $HOME; if it fails
    # to start, remove these three lines.
    read_only: true
    tmpfs:
      - /tmp
      - /home/beeper

  backend:
    networks: [default, beeper]
  queue-short:
    networks: [default, beeper]
  queue-long:
    networks: [default, beeper]

networks:
  beeper:
    internal: true
  beeper-egress: {}

volumes:
  beeper-data:
```

Do not add `ports:` to `beeper`, and do not attach it to the network your reverse proxy uses. The
Frappe site reaches it at `http://beeper:23373`.

## 3. Sign in (no browser needed)

`beeper setup --email` against a running server and `beeper setup --oauth` do not work headless.
The e-mail code flow does. Run the Beeper CLI once in a throwaway container on Beeper's egress
network (it reaches the `beeper` service there and can download the CLI), with its config in a
tmpfs so nothing stays behind:

```bash
docker run --rm -it --network <project>_beeper-egress \
  --tmpfs /cli -e BEEPER_CLI_CONFIG_DIR=/cli \
  node:24-slim bash
```

**Unverified:** the one-off container above (we ran the same CLI from a workstation through a
port forward), and the exact flags of `targets add remote`; check `beeper targets add --help`.

Inside the container run `npm i -g beeper-cli@0.6.2` (pin the version you tested), then:

1. `beeper targets add remote --name frappe --url http://beeper:23373`
2. `beeper auth email start --email you@example.com -t frappe --json`: Beeper mails you a code.
   This works only on a server that is not signed in yet (409 otherwise).
3. `beeper auth email response --setup-request-id <id from step 2> --code <code> -t frappe`
4. `beeper verify start -t frappe`, accept the request on a device that is already signed in.
5. `beeper verify show --json -t frappe` shows the emoji; compare them with that device.
6. `beeper verify sas-confirm --id <verification id> -t frappe`
7. Wait until the server reports `ready` (`beeper doctor -t frappe`, or
   `GET /v1/app/setup` with the token). The first sync took about 6 minutes in our setup and moves
   from `needs-first-sync` to `ready`.

Step 3 stores a Bearer token in the CLI target (`beeper targets show --json -t frappe`, **unverified**
command for reading it back). Copy it before you leave the container; the tmpfs is gone afterwards.

## 4. Connect chatsync

In Frappe open **Chat Account**, choose the user, set the base URL `http://beeper:23373`, paste
the token into **Access Token** (a password field) and tick **Enabled**. Then continue with the
setup in the [README](../README.md#setup).

## Security and operations

- **The token is the account's Matrix access token.** It works against Beeper's homeserver from
  anywhere, not only inside your network, and can read encrypted messages, send messages and manage
  devices. Keep it only in the Chat Account. On a leak: sign the device out in Beeper's settings,
  sign in again (step 3) and update the Chat Account.
- **Never expose port 23373.** Only the Frappe backend and workers should reach it.
- **Back up the volume.** `/data` holds the end-to-end encryption keys and your private messages.
  Encrypt backups. Restoring a backup avoids a new sign-in and device verification.
- **Use local disk.** The first sync is fsync-heavy (SQLite); on slow network storage it can take
  hours instead of minutes. State was ~390 MB after the first sync.
- **One Beeper Server per user.** Each server is signed in as one Beeper account; give every user
  who connects chats their own service, volume and token.
