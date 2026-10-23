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

## October 2026 invoice recovery

- Czech PrestaShop PDF invoices with a usable text layer are read by `EasyFlex/pdf_text.py` using the existing Poppler `pdftotext` binary. Unknown layouts fall back to vision. Source pages are retained in `invoice_data.source_pages`; contradictory printed summaries are flagged for review.
- Import failures belong in `last_import_error`, not the extraction `error` field. Legacy failed rows whose two fields match remain retryable. Imported, running, skipped and `merged` rows cannot be selected. `merged` retains historical duplicate rows without displaying or importing them.
- Partner lookup and snapshots use only the buyer for issued invoices and only the supplier for received invoices. Never fall back to identifiers from the other party; ambiguous directory ties remain unlinked and use the document snapshot.
- A job's company, direction and document type are explicit. Worker preflight reads the company's live document-type list before sending invoices. Text-verified amounts use explicit per-rate bases and tax, with authorised rounding represented as a separate accounting position.
- Error dumps persist in `/app/instance/errors` through `EASYFLEX_ERRORS_DIR` and the existing instance volume. Filenames include row and attempt time.
- Batch 146 recovery evidence and temporary isolated source/test copy: `/srv/projects/easyflex-webapp/output/batch146-recovery/` (not versioned). No additional public service or port. It contains the original failed request archive, prepared data and dry-run/persisted verification reports; these include customer data and must not be published or committed.
- Pre-change backup: `backups/easyflex_snapshot_20261023T135755Z/`. Preserve batch 146's original rows as an audit JSON before repair. Do not delete historical duplicate rows; mark them `merged` and retain their original payload.
- Regression tests: `tests/test_invoice_recovery.py`; run with the normal full suite in an isolated container/database. Never run tests against the production database. Deployment remains the Compose command above.
