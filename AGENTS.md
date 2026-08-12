# EasyFlex on Baller

- Project directory: `/srv/projects/easyflex-webapp`
- Live branch: `feature/receipt-import-test`
- Public endpoint: `https://easyflex.vvitovec.com`
- Runtime: Docker Compose project `easyflex-webapp`
- Containers: `easyflex-web`, `easyflex-worker`, `easyflex-db`, `easyflex-cloudflared`
- Internal web port: `10000` (Cloudflare Tunnel routes the public endpoint to the web container)
- Persistent data: Docker volumes `easyflex-webapp_postgres_data` and `easyflex-webapp_easyflex_instance`
- Configuration and secrets: `.env` (never commit or print its values)

## Deploy

From the project directory, validate with `docker compose config -q`, then deploy with:

```bash
docker compose --profile cloudflare up -d --build
```

The helper `scripts/selfhost/update_stack.sh` pulls the current branch first and therefore requires a clean worktree.

## Verify

```bash
docker compose ps
docker logs --since 5m easyflex-web
docker logs --since 5m easyflex-worker
curl -fsS https://easyflex.vvitovec.com/readyz
```

Run tests in the application image with `PYTHONPATH=/app pytest -q`.
