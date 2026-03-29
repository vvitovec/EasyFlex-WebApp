web: EASYFLEX_ENV=production gunicorn "webapp.app:app" --bind 0.0.0.0:${PORT:-8000} --timeout 180 --graceful-timeout 30 --workers ${WEB_CONCURRENCY:-2}
worker: EASYFLEX_ENV=production python -m webapp.worker
