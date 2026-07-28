from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


CODES = (
    "bb90",
    "bb108",
    "bb144",
    "bb288",
    "hgp_r5",
    "hgp_r9",
    "hgp_r13",
    "hgp_r17",
    "hgp_rb5_b2",
    "hgp_rb7_b2",
    "hgp_rb9_b2",
    "hgp_rb11_b2",
)
METHODS = ("bp_osd", "relay_050", "relay_lossy_dem_050", "relay_ours_050")
P_TOTALS = tuple(value / 1000 for value in range(4, 10))


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def _point_key(code: str, method: str, p_total: str | float) -> tuple[str, str, float]:
    return code, method, round(float(p_total), 6)


def build_snapshot(source_root: Path) -> dict[str, object]:
    results_root = source_root / "exp/eval/main-sweep/results"
    canonical_path = results_root / "eval_sweep_combined.csv"
    audit_path = results_root / "strict_convergence_gap_report.csv"

    canonical = {
        _point_key(row["code_label"], row["method_label"], row["p_total"]): row
        for row in _rows(canonical_path)
    }
    audit = {
        _point_key(row["code_label"], row["canonical_method_label"], row["p_total"]): row
        for row in _rows(audit_path)
    }

    rows: list[list[object]] = []
    for code in CODES:
        for method in METHODS:
            for p_total in P_TOTALS:
                key = _point_key(code, method, p_total)
                if key not in canonical:
                    raise ValueError(f"canonical snapshot is missing {key}")
                result = canonical[key]
                review = audit.get(key)
                per_shot_count = None
                if review is not None and review["status"] != "equivalent_no_rerun":
                    if review["status"] != "legacy_per_shot_incomplete":
                        raise ValueError(f"unsupported review status for {key}: {review['status']}")
                    per_shot_count = int(review["per_shot_count"])
                rows.append(
                    [
                        code,
                        method,
                        p_total,
                        int(result["num_shots"]),
                        int(result["logical_errors"]),
                        float(result["ler"]),
                        float(result["ler_per_round"]),
                        float(result["convergence_rate"]),
                        float(result["wall_time"]),
                        per_shot_count,
                    ]
                )

    if len(rows) != 288:
        raise ValueError(f"expected 288 canonical rows, found {len(rows)}")
    review_count = sum(row[-1] is not None for row in rows)
    if review_count != 69:
        raise ValueError(f"expected 69 evidence-review rows, found {review_count}")

    return {
        "schema_version": 1,
        "source": "exp/eval/main-sweep/results/eval_sweep_combined.csv",
        "audit_source": "exp/eval/main-sweep/results/strict_convergence_gap_report.csv",
        "columns": [
            "code_label",
            "method",
            "p_total",
            "shots",
            "logical_errors",
            "ler",
            "ler_per_round",
            "convergence_rate",
            "wall_time",
            "legacy_per_shot_count",
        ],
        "rows": rows,
    }


def write_snapshot(snapshot: dict[str, object], destination: Path) -> None:
    header = {key: value for key, value in snapshot.items() if key != "rows"}
    lines = ["{"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, separators=(',', ':'))},")
    lines.append('  "rows": [')
    rows = snapshot["rows"]
    assert isinstance(rows, list)
    for index, row in enumerate(rows):
        suffix = "," if index + 1 < len(rows) else ""
        lines.append(f"    {json.dumps(row, separators=(',', ':'))}{suffix}")
    lines.extend(["  ]", "}", ""])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the bundled decoder experiment snapshot")
    parser.add_argument("source_root", type=Path, help="path to the decoder_atomloss project")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("web/src/decoderCanonicalSnapshot.json"),
    )
    args = parser.parse_args()
    write_snapshot(build_snapshot(args.source_root.resolve()), args.output)


if __name__ == "__main__":
    main()
