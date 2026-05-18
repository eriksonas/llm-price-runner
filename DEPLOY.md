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

## 3. Copy project to VPS

```bash
scp -r /path/to/llm_price_runner root@69.62.127.135:/opt/llm-price-runner
```

## 4. Build and start

```bash
ssh root@69.62.127.135
cd /opt/llm-price-runner
docker compose up -d --build
docker compose logs -f    # watch for startup + cert issuance
```

The app will be live at **https://models.agent-startup.com** once Let's Encrypt issues the cert (30–60 seconds on first start).

## 5. Verify

```bash
curl -I https://models.agent-startup.com/api/meta
# Should return HTTP/2 200
```

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
