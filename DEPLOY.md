# Deploy to Hostinger VPS with Traefik + HTTPS

## Prerequisites
- VPS at 69.62.127.135 with Traefik already running
- DNS A record: `models.agent-startup.com → 69.62.127.135`
- Traefik configured with a `letsencrypt` cert resolver and `websecure` entrypoint

## 1. Set DNS A record

In your DNS panel (Hostinger or wherever agent-startup.com is managed):
```
models.agent-startup.com  A  69.62.127.135  TTL 300
```
Wait for propagation (usually < 5 min with TTL 300). Verify:
```bash
nslookup models.agent-startup.com
```

## 2. Check your Traefik network name

SSH into the VPS and run:
```bash
docker network ls
```
Look for the network Traefik is attached to (commonly `traefik` or `proxy`).

If it's not `traefik`, edit `docker-compose.yml` — change both occurrences:
```yaml
networks:
  traefik:        # ← change this to your network name
    external: true
```
and the label:
```yaml
- "traefik.docker.network=traefik"   # ← change to match
```

## 3. Get the code on the VPS

Clone from GitHub (preferred — versioned, only the diff transfers on update):

```bash
ssh root@69.62.127.135
cd /opt
git clone https://github.com/eriksonas/llm-price-runner.git
cd llm-price-runner
```

Or `scp -r` from a local working tree if you're iterating without pushing:

```bash
scp -r /path/to/llm_price_runner root@69.62.127.135:/opt/llm-price-runner
```

## 4. Build and start

```bash
docker compose up -d --build
docker compose logs -f    # watch for startup + cert issuance
```

The app will be live at **https://models.agent-startup.com** once Let's Encrypt issues the cert (30–60 seconds on first start).

## 5. Verify

```bash
curl -I https://models.agent-startup.com/healthz
# Should return HTTP/2 200
```

## Updating a running deployment

```bash
cd /opt/llm-price-runner
git pull
docker compose up -d --build    # rebuilds the image, recreates the container
docker compose logs -f --tail=30
```

The named `price_data` volume survives, so the SQLite override + history
table is preserved across deploys.

### One-time chown if upgrading from a pre-non-root build

The current Dockerfile runs as an unprivileged user (uid 1000). If your
existing `price_data` volume was created by an older root-running
container, the new container can't write to it and you'll see
`sqlite3.OperationalError: attempt to write a readonly database` in the
startup logs. Fix it once with a throwaway container:

```bash
docker compose down
docker run --rm -v llm-price-runner_price_data:/data alpine chown -R 1000:1000 /data
docker compose up -d
```

Fresh deploys don't need this — Docker preserves the Dockerfile's
chown when creating a brand-new volume.

## Troubleshooting

**Cert not issuing:**
```bash
docker logs traefik 2>&1 | grep -i "models.agent-startup"
```
Make sure the DNS A record is live before starting the container.

**Wrong network name:**
```bash
docker inspect traefik | grep -A5 '"Networks"'
```
Use whatever network Traefik itself is on.

**HTTP redirect not working:**
The `redirect-https@docker` middleware must exist in your Traefik config.
If it doesn't, replace the HTTP router labels with:
```yaml
- "traefik.http.middlewares.llm-https-redirect.redirectscheme.scheme=https"
- "traefik.http.routers.llm-price-runner-http.middlewares=llm-https-redirect"
```

## Weekly RTT refresh (cron)

`tools/run_probes.py` measures RTT from RIPE Atlas probes near each user
city to each region hub, then writes medians to
`app/data/rtt_measured.json`. The running container picks up changes
automatically via the read-only bind mount of `app/data/` declared in
`docker-compose.yml` — no rebuild or restart needed.

### One-time host setup

```bash
ssh root@69.62.127.135
cd /opt/llm-price-runner

# Install Python + venv on the VPS host (outside docker).
apt update && apt install -y python3-venv python3-pip

# Create a dedicated venv for the cron job. `.venv-probes/` is
# gitignored (covered by `.venv*` in .gitignore).
python3 -m venv .venv-probes
.venv-probes/bin/pip install httpx python-dotenv

# Make sure .env has RIPE_ATLAS_KEY (one-line file alongside compose):
grep RIPE_ATLAS_KEY .env || echo "RIPE_ATLAS_KEY=atlas_key_here" >> .env

# Sanity check: dry-run should print the credit estimate without
# spending credits.
.venv-probes/bin/python -m tools.run_probes --dry
```

### Cron entry

Edit root's crontab (`crontab -e`) and add:

```cron
# Refresh measured RTTs weekly, Sunday 03:00 Europe/Vilnius (VPS TZ).
0 3 * * 0 /opt/llm-price-runner/tools/cron_probes.sh
```

The wrapper script logs to `/var/log/probes.log` (creates it on first
run). Tail it after the first scheduled run to confirm it worked:

```bash
tail -50 /var/log/probes.log
```

Each weekly run costs ~6 200 RIPE Atlas credits, well under typical
account budgets. The script is idempotent and merges with the existing
file, so a failed run never destroys data.

### Verifying the bind mount works

After the next refresh, the container should see the new RTTs without
a restart. Check by hitting `/api/models?city=vilnius` and confirming
the `network_rtt_ms` field for a few models matches what's currently in
`app/data/rtt_measured.json` on the host.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/models?category=code` | All models, optional category filter |
| `GET /api/best` | Best EU value per category |
| `GET /api/history/{model_id}` | Price history for a model |
| `POST /api/update` | Update a model's price manually |
| `POST /api/refresh` | Trigger immediate data refresh |
| `GET /api/meta` | Metadata (categories, colors, counts) |

## Update a price manually

```bash
curl -X POST https://models.agent-startup.com/api/update \
  -H "Content-Type: application/json" \
  -d '{"id":"openai-gpt-4o","input_usd_per_1m":2.50,"output_usd_per_1m":10.00,"notes":"Verified 2026-04-17"}'

# `id` is the catalogue slug from app/data/providers.py (e.g. "openai-gpt-4o"),
# not the provider's API model_id (e.g. "gpt-4o").
```

## Add a new model

Edit `app/data/providers.py`, add an entry to `PROVIDERS`, then:
```bash
docker compose up -d --build
```
