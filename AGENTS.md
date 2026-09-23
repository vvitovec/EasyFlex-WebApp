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

## September 2026 maintenance

- PostgreSQL is pinned to `17.11-alpine`; web dependencies are constrained by `webapp/requirements.lock`. Refresh the lock deliberately after testing dependency updates.
- `invoice_row.source` uses TEXT. Startup widens legacy PostgreSQL VARCHAR under the existing migration lock, retaining source labels and invoice data.
- PostgreSQL upload regression: set `EASYFLEX_TEST_POSTGRES_URL` to an isolated database named `easyflex_regression`, then run `PYTHONPATH=/app pytest tests/test_upload_postgres.py -q`. Never point it at production. Ordinary test runs skip these two tests when the URL is absent.
- User systemd `easyflex-backup.timer` runs nightly at 03:15 plus up to ten minutes. Linger is enabled for `viktoor`. Units are in `~/.config/systemd/user/`; source copies are in `scripts/selfhost/systemd/`.
- Backups: `/srv/projects/easyflex-webapp/backups/easyflex_snapshot_<UTC timestamp>/`, retention 14 days, permissions 700/600. Includes DB, instance files, environment configuration, revision, and checksums. Backups are local to Baller, not offsite; avoid concurrent imports when taking a coordinated pre-deploy snapshot.
- Check backup runs with `systemctl --user status easyflex-backup.service` and `journalctl --user -u easyflex-backup.service`. Restore-check only in an isolated PostgreSQL database with no worker attached.
