"""Cleanup old batch storage artifacts."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_RETENTION_HOURS = 72


def cleanup_old_batch_dirs(root: Path, *, retention_hours: int) -> int:
	if not root.exists():
		return 0
	cutoff = datetime.now() - timedelta(hours=max(1, retention_hours))
	removed = 0
	for child in root.iterdir():
		if not child.is_dir():
			continue
		mtime = datetime.fromtimestamp(child.stat().st_mtime)
		if mtime >= cutoff:
			continue
		shutil.rmtree(child, ignore_errors=True)
		removed += 1
	return removed


def main() -> None:
	project_root = Path(__file__).resolve().parent.parent
	instance_root = project_root / "instance" / "batches"
	retention_hours = int(os.getenv("BATCH_STORAGE_RETENTION_HOURS", str(DEFAULT_RETENTION_HOURS)))
	removed = cleanup_old_batch_dirs(instance_root, retention_hours=retention_hours)
	print(f"Removed {removed} old batch directories from {instance_root}")


if __name__ == "__main__":
	main()
