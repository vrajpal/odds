# Deploy: Tailscale sidecar (ScaleTail pattern)

Runs the odds APIs as their own tailnet node named `odds`, following
[tailscale-dev/ScaleTail](https://github.com/tailscale-dev/ScaleTail):
a `tailscale/tailscale` sidecar owns the network namespace, the app
containers share it (`network_mode: service:tailscale`), and Tailscale
Serve terminates HTTPS with automatic certs. No host ports are published;
the APIs bind loopback inside the namespace, so Serve is the only door.

- `https://odds.<tailnet>.ts.net` — contest API (Swagger at `/docs`)
- `https://odds.<tailnet>.ts.net:8443` — MLB odds API + web UI

Because the tailnet identity lives in the `tailscale-state` volume, the whole
stack is host-portable: bring it up on any machine and it is the same `odds`
node.

## Bring-up (any Docker host)

```bash
git clone https://github.com/vrajpal/odds && cd odds/deploy
cp .env.example .env    # fill TS_AUTHKEY + THE_ODDS_API_KEY
docker compose up -d --build
docker compose run --rm nfl-collect     # seed NFL lines (3 credits)
```

Existing databases can be seeded by copying them into `deploy/data/` before
first start (`odds.sqlite`, `nfl-odds.sqlite`).

Collection cadence during the season (host crontab, Thu–Sat 3x/day ≈ 27
credits/week):

```cron
0 8,13,18 * * 4-6  cd /opt/odds/deploy && docker compose run --rm nfl-collect
```

## Fresh VM provisioning (cloud-init)

For a dedicated VM (Proxmox, cloud, wherever), this cloud-init user-data gets
from blank image to running stack; only `.env` needs filling afterwards:

```yaml
#cloud-config
package_update: true
packages: [git, ca-certificates, curl]
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - git clone https://github.com/vrajpal/odds /opt/odds
  - cp /opt/odds/deploy/.env.example /opt/odds/deploy/.env
  # edit /opt/odds/deploy/.env, then: cd /opt/odds/deploy && docker compose up -d --build
```

## Notes

- `TS_AUTHKEY` enrolls the node once; state persists in the `tailscale-state`
  volume and the key is not needed again. Rotate/revoke from the admin console
  like any other device.
- `AllowFunnel` is explicitly `false` — these APIs have no auth and must never
  be exposed to the public internet (Funnel would do exactly that).
- The sidecar needs `/dev/net/tun` and `net_admin` (kernel networking). In an
  unprivileged LXC/LXD container, enable nesting + tun, or set
  `TS_USERSPACE: "true"` (works everywhere, slightly slower).
