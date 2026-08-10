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

**Updating a running deployment:**
`git pull && docker compose --profile collect --profile public build &&
docker compose --profile public up -d`. The profile flags on `build` matter:
bare `docker compose build` (and `up -d --build`) skip profile-gated
services entirely, so the collect one-shots silently keep an old image —
this bit twice (D-027 deploy; D-037's migration never applying because
nfl-results ran pre-migration code).

Season cadence (host crontab). Thu–Sat line polls ≈ 27 credits/week; the
Sunday 9:55 AM PT poll captures true closing lines for CLV (D-024); results
runs are free:

```cron
0 8,13,18 * * 4-6  cd /opt/odds/deploy && docker compose run --rm nfl-collect
55 9 * * 0         cd /opt/odds/deploy && docker compose run --rm nfl-collect
0 21 * * 0         cd /opt/odds/deploy && docker compose run --rm nfl-results
0 7 * * *          cd /opt/odds/deploy && docker compose run --rm mlb-projections
30 8 * * 2         cd /opt/odds/deploy && docker compose run --rm nfl-results
# Survivor holiday legs (D-028): TG is decided Tue-Wed (deadline Wed Nov 25
# 4 PM PT), XMAS on Wed (Thu polls exist via the 4-6 rule) — without these,
# holiday picks would be made on stale Sunday lines. ~18 credits/season.
0 8,13 24 11 *     cd /opt/odds/deploy && docker compose run --rm nfl-collect
0 8,12 25 11 *     cd /opt/odds/deploy && docker compose run --rm nfl-collect
0 8,13 23 12 *     cd /opt/odds/deploy && docker compose run --rm nfl-collect
```

(9:55 AM Sunday = just before the early window; 9 PM Sunday catches the day's
finals; Tuesday morning sweeps MNF and any stragglers. Holiday entries are
shown in PT here — the deployed host runs ET, so its crontab carries the
ET conversions.)

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

## Operations quick reference

```bash
# All commands from ~/containers/odds/deploy on apps.
docker compose --profile public ps                 # what's running
docker compose --profile public up -d              # start/refresh everything
git pull && docker compose --profile collect --profile public build && docker compose --profile public up -d   # deploy an update
docker compose run --rm nfl-collect                # manual line poll (3 credits)
docker compose run --rm nfl-results                # manual finals sweep (free)
docker compose run --rm statcast                   # daily MLB scouting pull (free, D-031)
docker compose logs -f contest-api                 # follow app logs
tail -f ~/containers/odds/collect.log              # cron output
```

Always pass `--profile public` once the tunnel is in use — compose ignores
profiled services otherwise, and a plain `up -d` will treat cloudflared as an
orphan.

Health checks, all from any tailnet machine:

```bash
curl https://odds.<tailnet>.ts.net/api/contest/health          # app up
curl -o /dev/null -w '%{http_code}\n' https://<public-domain>/ # 302 = Access wall up
cloudflared tunnel info odds                                   # connector registered?
curl -H "Cf-Access-Authenticated-User-Email: <email>" \
  https://odds.<tailnet>.ts.net/api/contest/whoami             # identity mapping
```

## Troubleshooting

Every entry below is a failure mode this deployment has actually hit.

**Tailscale SSH to the host hangs, then "failed to fetch next SSH action".**
Tailscale SSH check mode: each session needs browser approval (valid ~12h),
and the approval URL dies with its session after ~2 minutes. Approve fast, or
run the commands from an interactive terminal where the check flows natively.

**Sidecar stuck unhealthy, login URL keeps changing.** Without TS_AUTHKEY,
containerboot times out waiting for the interactive login, exits, restarts,
and mints a new URL — a race humans lose. Use a pre-approved auth key in
.env instead (one enrollment, then state persists in the tailscale-state
volume; revoke the key after).

**cloudflared restart-loops: "couldn't read tunnel credentials: permission
denied".** The image runs as a non-root user; the mounted credentials must be
world-readable (dir 755, files 644) or chowned to the container UID.

**Public domain serves Cloudflare error 1033 / tunnel unreachable.** Check
`cloudflared tunnel info odds` — no connector means the container isn't
running or can't reach Cloudflare. The Access 302 comes from the edge, so a
working login wall does NOT prove the tunnel is up.

**A one-shot service errors with an option/feature that just shipped.**
`up -d --build` AND bare `docker compose build` both skip profile-gated
services; the one-shots keep their old image. Always build with
`--profile collect --profile public`. Related: a newly deployed schema
migration stays unapplied until something write-opens the database — the
APIs are read-only by design (D-012) and 500 with "no such table" until a
one-shot (`docker compose run --rm nfl-results`) touches it.

**Host suddenly cannot resolve any hostname (git pull, API calls fail); SSH
still works.** /etc/resolv.conf points solely at Tailscale MagicDNS
(100.100.100.100). Confirm with `nslookup github.com 1.1.1.1` (works = DNS,
not network) then `sudo systemctl restart tailscaled`. Container DNS forwards
to the host resolver, so crons and the tunnel fail on new lookups too until
this is fixed.

**Garbage member name in the UI dropdown / identity mapping dead.** Two
.env pitfalls: (1) appending with `echo >>` to a file without a trailing
newline glues onto the last line — check `tail -c1 .env | xxd`; (2) a
variable in .env reaches a container ONLY if the service's `environment:`
declares it — `docker exec odds-contest-api-1 env | grep CONTEST` shows the
truth. Compose interpolation succeeding proves nothing about the container.

**Cron fires at the wrong hour.** This host runs America/New_York; crontab
times are ET conversions of PT intents (see the crontab comments). Re-derive
before editing — the Sunday closing poll must land ~9:55 AM PT, and Sunday
9 PM PT is Monday 00:00 ET.

**Access application won't save: "allow_authenticate_via_warp cannot be
set...".** Turn off the "WARP authentication identity" toggle in the
application's login-methods section (or set a Cloudflare One session duration
under Access > Settings). WARP is not used here.

**A friend passes the login wall but gets 403 "not mapped" when acting.**
Their email is in the Access policy but not in CONTEST_MEMBER_EMAILS (or
spelled differently). Matching is case-insensitive; fix the mapping and
`docker compose --profile public up -d contest-api`.

**Contest board 503 "NFL odds database unavailable".** The read-only open
found no database file — run one collect (`docker compose run --rm
nfl-collect`) and it appears; the API never creates databases by design.
