# Public access: https://climate.apprelay.net

This puts Climate AI on a public name through the GrowWise **AppRelay** gateway, so the app,
the iPhone app and API tokens work away from home without Tailscale. No router port is opened:

```
phone / laptop ──https──> Cloudflare ──tunnel──> apprelay-nginx ──http──> climate-ai-app:8000
                  (TLS ends here)     (outbound from     (on the shared Docker
                                       the box)           network apprelay_gateway)
```

Only the `app` container joins the shared network. The database, the agent, the MCP server and
ntfy stay on Climate's own networks and their ports stay on `127.0.0.1`.

This makes a thermostat controller reachable from the internet behind one password. That is
reasonable with the protections below, but it is a choice: **Tailscale** gives you the same
away-from-home access with nothing public at all ([DEPLOY.md](DEPLOY.md) section 9).

## The path, in order

### 1. The gateway change

The gateway side lives in the GrowWise repo: pull request
[duanekaeb/growwise-ai#8](https://github.com/duanekaeb/growwise-ai/pull/8). It adds:

- the manifest line `climate  climate.apprelay.net  single  web=climate-ai-app:8000  extras=yes`
  in `apprelay/gateway/apps.conf`;
- Climate's extras, `apprelay/gateway/nginx/snippets/apps/climate.conf`: plain http
  redirected to https, HSTS, a per-address rate limit on the sign-in endpoints, and 404 for the
  API docs;
- a shared `signin` rate-limit zone (`nginx/conf.d/10-rate-limits.conf`), and a fallback in
  `validate.sh` so `make reload` works on a box with no host nginx.

Merging it changes nothing on the box yet (step 4 does).

### 2. Choose the owner password, from home, first

Before the name goes live, choose the password from a private address: the first-run screen
over an SSH tunnel or your home network, or `make password` on the server
([DEPLOY.md](DEPLOY.md) section 4). The app refuses first-run setup from internet addresses
anyway, but the order should not depend on that. Use a long, unique passphrase from your
password manager; it is the one thing standing between the internet and your thermostats.

Keep `CLIMATE_ALLOW_REMOTE_SETUP` unset (false).

### 3. Climate's `.env`

```
COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml
NGINX_NETWORK=apprelay_gateway
CLIMATE_PUBLIC_URL=https://climate.apprelay.net
CLIMATE_COOKIE_SECURE=true
FORWARDED_ALLOW_IPS=<the subnet of apprelay_gateway>
```

Get the subnet with:

```bash
docker network inspect apprelay_gateway -f '{{(index .IPAM.Config 0).Subnet}}'
```

Then `docker compose up -d` (or `make update`). `docker network inspect apprelay_gateway`
should now list `climate-ai-app-1`. `make doctor` checks the secrets and flags an https
`CLIMATE_PUBLIC_URL` with `CLIMATE_COOKIE_SECURE` still false.

**Why the subnet and not the nginx container's IP** is in
[The one setting that can undo all of this](#the-one-setting-that-can-undo-all-of-this) below.

### 4. Apply the gateway on the box

From an up-to-date checkout of growwise-ai with the pull request merged (try the rsync with
`-n` first to see what it would change):

```bash
rsync -av --exclude .env --exclude cloudflared/ --exclude nginx/conf.d/generated/ \
  apprelay/gateway/ /srv/apprelay/gateway/
make -C /srv/apprelay reload     # generate, nginx -t, reload without dropping connections
```

This reload is the moment the name goes live.

### 5. DNS

Nothing to do in the usual case: the tunnel's wildcard `*.apprelay.net` already covers
`climate.apprelay.net`. Check:

```bash
dig +short climate.apprelay.net        # Cloudflare addresses
```

If it does not resolve, add the one name (harmless either way):
`cloudflared tunnel route dns apprelay climate.apprelay.net`.

### 6. Check it from outside

```bash
curl -sS -D - -o /dev/null https://climate.apprelay.net/api/health    # 200 + Strict-Transport-Security
curl -sS -o /dev/null -w '%{http_code}\n' http://climate.apprelay.net/  # 301 (to https)
curl -sS -o /dev/null -w '%{http_code}\n' https://climate.apprelay.net/api/docs   # 404
```

Then the check that matters most: on your phone, **turn Wi-Fi off**, open
`https://climate.apprelay.net`, sign in, and look at **More > Security > Signed-in devices**.
This phone must show your carrier's public address. If it shows a `172.x`, `10.x` or
`192.168.x` address, the app is treating internet visitors as home: take the name offline
([below](#taking-it-offline-in-a-hurry)) and fix `FORWARDED_ALLOW_IPS`.

### 7. The iPhone app

Settings > server address: `https://climate.apprelay.net` (a dotted name defaults to https).
"Test and save" checks `/api/health`, then sign in once; the device stays signed in (see below).

### 8. API tokens for services outside your home

A service that calls in from outside (a dashboard hosted elsewhere, a phone shortcut on mobile
data) needs a token with **"Home network only" switched off**: More > Security > API tokens,
or `make token NAME=dashboard ROLE=viewer REMOTE=1`. Give it the smallest role that works
(`viewer` reads, `control` adds holds and presence), an expiry, and its own name so you can
revoke it alone. Send it as `Authorization: Bearer cai_...`; the gateway passes the header
through. Keep `agent` tokens home-only: the agent and MCP server run on the box.

## What protects it, and how

| | How |
|---|---|
| **One owner password** | No user accounts. Stored only as an Argon2id hash, never logged, never sent back. New passwords need 10+ characters. Changing it signs out every other device. |
| **Real, short-lived tokens** | Signing in returns a 15-minute bearer token that the web app keeps in memory only, plus a refresh cookie (HttpOnly, `Secure`, `SameSite=Lax`, sent only to `/api/auth`). Every request re-checks the device's session, so signing a device out takes effect on its next request. |
| **Per-device sessions you can revoke** | Each phone, tablet and browser is its own signed-in device: name, last seen, address, under More > Security. Sign out one or all. The refresh token rotates on every use; replaying an old one signs that device out. Devices expire after 30 days unused and 90 days at most. |
| **Guessing does not work, and cannot lock you out** | Argon2id on every attempt, at most two checks at once, 10 failures per address per 5 minutes. Failures from internet addresses are also counted together: past 20 in an hour, sign-in **from the internet** pauses for 15 minutes, with an audit entry and an alert. Sign-in from home or Tailscale always works, and signed-in devices are unaffected, so nobody can lock you out. |
| **Edge rate limit** | The gateway limits `/api/auth/login`, `setup`, `reauth`, `change-password` and `refresh` to 10 a minute per address (plus a small burst) before the app sees them, answering `429 RATE_LIMITED`. |
| **HTTPS only** | TLS ends at Cloudflare; the gateway redirects plain http to https and sends HSTS (one year, this hostname only), so after the first visit a browser never tries http again. The refresh cookie is `Secure`. |
| **First-run setup only from home** | The screen that chooses the first password is refused from internet addresses (`403 SETUP_NOT_ALLOWED`). |
| **Tokens stay home unless you say so** | API tokens are home-network-only by default. The agent and MCP service tokens from `.env` are accepted from private addresses only. No token is ever the owner: none can change settings, the controller mode or setup, or manage tokens. Creating a token needs the password entered within the last 10 minutes. |
| **Nothing else is published** | Only the app is on the gateway network. MCP, the database and ntfy never are. The API docs (`/api/docs`, `/api/openapi.json`) answer 404 on the public name and keep working at home. |
| **The house stays safe whoever is signed in** | Every thermostat write still goes through the hard limits in code (setpoint range, rate of change, timed holds only). If the server dies, the holds expire and each ecobee runs its own schedule. |
| **You can see what happened** | More > Security > Recent activity: sign-ins, failures, pauses, password, device and token changes. No passwords or tokens in it. |

## The one setting that can undo all of this

The app decides who is "at home" (first-run setup, home-only tokens, the service tokens, which
failures count toward the internet pause) from the client address. Behind the gateway that
address arrives in `X-Forwarded-For`, and uvicorn believes that header only from
`FORWARDED_ALLOW_IPS`.

If nginx's address is **not** covered, uvicorn ignores the header and the app sees every
visitor as nginx itself: a private Docker address, so **the whole internet counts as home**.
That fails open, silently. Hence:

- **Use the `apprelay_gateway` subnet**, not the nginx container's IP. The network outlives
  every container on it; the container's IP changes whenever it is recreated (an image update,
  a `docker compose up` after a config change), and from then on the old IP is trusted and the
  new one is not.
- The default, `172.16.0.0/12`, also covers the gateway (AppRelay's Docker pools sit inside
  it), only more broadly.
- Trusting the subnet means any container on `apprelay_gateway` could claim another address in
  `X-Forwarded-For`. Those containers (cloudflared, nginx, GrowWise, Portainer, Uptime Kuma)
  can reach `climate-ai-app:8000` directly anyway, and connecting directly from a Docker
  address already counts as home. The trust boundary is the box: keep only software you run
  yourself on that network.
- Re-check the signed-in devices list (step 6) after any change to the gateway or this setting.

## Home vs. the public name

Opening `https://climate.apprelay.net` from your own Wi-Fi still goes out to Cloudflare and
back, so the app sees your home's **public** address: the internet sign-in pause applies, and
home-only tokens are refused on that name. "Home always works" means the private ways in:
Tailscale, your home network (`APP_BIND=0.0.0.0`), an SSH tunnel, or `make password` on the
server. If an internet pause ever catches you, sign in one of those ways or wait 15 minutes.

The gateway's rate limit is per address too, and your whole household shares one public
address on the public name. Normal use is far below it.

## Limits worth knowing

- **Long requests:** Cloudflare gives a request 100 seconds to start answering, then shows a
  524. A very long backtest or simulation started on the public name can hit that; run it from
  home.
- **Many addresses:** the edge limit is per address, so an attacker with many addresses (IPv6
  makes that cheap) is not slowed by it. The app's internet pause is what covers that case.
- **Losing a phone:** More > Security > Signed-in devices > sign it out (or sign out everywhere
  from a computer), then change the password.

## Optional: Cloudflare Access in front

Cloudflare Access (an email one-time code at Cloudflare before anything reaches the gateway) is
a second, outer gate. It works in a browser and as a Safari home-screen app. **It breaks the
Climate AI iPhone app**: the app's web view keeps only the configured host and opens every other
host in Safari. The Access login page is on `<team>.cloudflareaccess.com`, so it opens in
Safari, the Access cookie lands in Safari, and the app itself never gets past the gate. Its
"Test and save" also fails, because the health check receives the Access login page. Services
using API tokens would need Access service tokens as well.

If you want it anyway:

- create a self-hosted Access application for `climate.apprelay.net` only (never one for
  `*.apprelay.net`: that would gate GrowWise too), with an Allow policy for your own email;
- add `access=require` to Climate's line in `apps.conf` (the gateway then refuses requests that
  did not come through Access), regenerate and reload;
- use Safari or the home-screen app instead of the iPhone app;
- keep the app's own login: Access is the outer gate, not a replacement.

## Taking it offline in a hurry

On the box, comment out the `climate` line in `/srv/apprelay/gateway/apps.conf`, then
`make -C /srv/apprelay reload`. The name answers the gateway's 404 within a second; the
generator warns that Climate's snippet is no longer applied, which is expected. The house keeps
running: nothing on Climate's side stops, and home access is unchanged.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `502 Bad Gateway` | The app is not on `apprelay_gateway`: check `COMPOSE_FILE` / `NGINX_NETWORK` in `.env`, `docker compose up -d`, then `docker network inspect apprelay_gateway`. |
| "Choose the password from your home network" | Expected on the public name. Choose it from home (step 2). |
| `429` with `RATE_LIMITED` | The gateway's per-address limit. Wait a minute. |
| `429` with `TOO_MANY_ATTEMPTS` | The app's own limit: 10 failures from one address in 5 minutes, or too many password checks queued at once. Wait a few minutes. |
| `429` with `LOGIN_PAUSED` | The app's internet sign-in pause after many failures. Check Recent activity; sign in from home or wait 15 minutes. |
| Signed-in devices show a `172.x` address for a phone on mobile data | `FORWARDED_ALLOW_IPS` does not cover nginx: [see above](#the-one-setting-that-can-undo-all-of-this). Take the name offline until fixed. |
| `524` from Cloudflare | A request took over 100 s to start answering: see Limits. |
