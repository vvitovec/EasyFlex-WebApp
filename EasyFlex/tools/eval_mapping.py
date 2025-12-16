"""
Jednoduchý eval harness pro mapování CSV → interní pole.
Použití:
    python -m EasyFlex.tools.eval_mapping --fixtures tests/fixtures
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd

from EasyFlex.csv_processor import CSVProcessor


def _load_expected(json_path: Path) -> Dict[str, str]:
	return json.loads(json_path.read_text(encoding="utf-8"))


def _evaluate_one(csv_path: Path, expected: Dict[str, str]) -> Tuple[int, int, Dict[str, str]]:
	df = pd.read_csv(csv_path)
	processor = CSVProcessor()
	mapping = processor._infer_column_map(df)
	correct = 0
	total = 0
	for target, expected_header in expected.items():
		total += 1
		if mapping.get(target) == expected_header:
			correct += 1
	return correct, total, mapping


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument("--fixtures", type=str, default="tests/fixtures", help="Složka s CSV + *_expected.json")
	args = parser.parse_args()
	fixture_dir = Path(args.fixtures)
	if not fixture_dir.exists():
		raise SystemExit(f"Fixtures {fixture_dir} nenalezeny")
	csv_files = sorted(fixture_dir.glob("mapping_case*.csv"))
	if not csv_files:
		raise SystemExit("Nenalezeny žádné fixture CSV (mapping_case*.csv)")
	total_correct = 0
	total_all = 0
	for csv_path in csv_files:
		json_path = csv_path.with_name(csv_path.stem + "_expected.json")
		if not json_path.exists():
			print(f"[WARN] Chybí expected mapping pro {csv_path.name}, přeskočeno.")
			continue
		expected = _load_expected(json_path)
		correct, total, mapping = _evaluate_one(csv_path, expected)
		total_correct += correct
		total_all += total
		print(f"{csv_path.name}: {correct}/{total} správně")
		for target, header in sorted(mapping.items()):
			print(f"  {target:20s} -> {header}")
		print("----")
	if total_all:
		accuracy = total_correct / total_all
		print(f"Celková přesnost: {total_correct}/{total_all} = {accuracy:.2%}")


if __name__ == "__main__":
	main()
