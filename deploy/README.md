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

Season cadence (host crontab). Thu–Sat line polls ≈ 27 credits/week; the
Sunday 9:55 AM PT poll captures true closing lines for CLV (D-024); results
runs are free:

```cron
0 8,13,18 * * 4-6  cd /opt/odds/deploy && docker compose run --rm nfl-collect
55 9 * * 0         cd /opt/odds/deploy && docker compose run --rm nfl-collect
0 21 * * 0         cd /opt/odds/deploy && docker compose run --rm nfl-results
30 8 * * 2         cd /opt/odds/deploy && docker compose run --rm nfl-results
```

(9:55 AM Sunday = just before the early window; 9 PM Sunday catches the day's
finals; Tuesday morning sweeps MNF and any stragglers.)

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

## Public access for non-tailnet friends (Cloudflare, D-026)

The `public` profile adds a `cloudflared` tunnel inside the sidecar's network
namespace — still zero published ports — with Cloudflare Access (email OTP
allowlist) as the login layer. No code runs a login flow; Access is the door.

One-time dashboard setup (one.dash.cloudflare.com, free plan):

1. **Tunnel** (already done via CLI for this deployment): a locally-managed
   tunnel whose `config.yml` + credentials json live in `deploy/cloudflared/`
   (gitignored). To recreate elsewhere: `cloudflared tunnel login`,
   `tunnel create odds`, `tunnel route dns odds <domain>`, copy
   `~/.cloudflared/<uuid>.json` beside a config.yml pointing ingress at
   `http://localhost:8001`.
2. **Access**: Access → Applications → Add → Self-hosted. Application domain:
   your domain (apex works). Add a policy: Action *Allow*, Include → Emails →
   the three members' addresses. Session duration to taste (e.g. 1 month).
   Identity provider: One-time PIN is enabled by default — that's the email
   code flow, no accounts needed.
3. **Identity mapping**: in `deploy/.env` set
   `CONTEST_MEMBER_EMAILS=email:Member,email:Member,email:Member`
   (members must match `CONTEST_MEMBERS`). Mapped visitors get their member
   locked in the UI and cannot act as anyone else; unmapped-but-allowed
   emails are read-blocked from acting (403).
4. `docker compose --profile public up -d`

The tailnet URL keeps working unchanged (no header → member dropdown).
Header trust note: `Cf-Access-Authenticated-User-Email` is trustworthy here
because the only non-tailnet path into the app is the tunnel itself; if that
ever changes, upgrade to verifying Cloudflare's signed JWT
(`Cf-Access-Jwt-Assertion`) instead.
