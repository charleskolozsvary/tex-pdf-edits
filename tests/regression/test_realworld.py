import pytest
import argparse
import subprocess

from run_realworld import RESULTS_DIR, CSV_COLUMNS
from pathlib import Path

BENCHMARK_PATH = RESULTS_DIR / 'realworld_passed.csv'

ID_FIELD_IDX = CSV_COLUMNS.index('id')

ANNOT_RETENTION = 0.98
EDIT_RETENTION = 0.70

def get_latest_path():
    return Path(max(
        file for file in RESULTS_DIR.iterdir()
        if file.suffix == '.csv' and file.name != BENCHMARK_PATH.name
    ))

def load_results(path: Path):
    with open(path, 'r') as f:
        lines = f.readlines()
    if not lines:
        raise RuntimeError(f"File {path} is empty")
    fields = lines[0].split(',')
    results = {}
    for line in lines[1:]:
        values = line.split(',')
        run_id = values[ID_FIELD_IDX]
        results[run_id] = {
            field : value for field, value in zip(CSV_COLUMNS, values)
        }
    return results

def get_benchmarks():
    if not BENCHMARK_PATH.exists():
        return {}
    else:
        return load_results(BENCHMARK_PATH)

def test_regressions():
    latest_path = get_latest_path()
    print(f"Evaluating {latest_path} against {BENCHMARK_PATH}...")    
    latest_runs = load_results(latest_path)
    benchmarks = get_benchmarks()
    for run_id, run in latest_runs.items():
        if run_id not in benchmarks:
            evaluate_no_benchmark(run_id, run)
            continue
        evaluate_with_benchmark(run_id, run, benchmarks[run_id])

def floatify(*arr):
    return [
        float(a) for a in arr
    ]
    
        
def evaluate_with_benchmark(run_id, run, benchmark):
    prev_annots, prev_edits, prev_corrs, prev_autos = floatify(
        benchmark['n_annots'],
        benchmark['n_edits'],
        benchmark['n_corrs'],
        benchmark['n_autos'],
    )
    n_annots, n_edits, n_corrs, n_autos = floatify(
        run['n_annots'],
        run['n_edits'],
        run['n_corrs'],
        run['n_autos'],
    )
    assert prev_annots == n_annots
    prev_annot_retention = prev_edits / prev_annots
    prev_edit_retention = prev_corrs / prev_edits
    prev_auto_rate = prev_autos / prev_corrs
    
    annot_retention = n_edits / n_annots
    edit_retention = n_corrs / n_edits
    auto_rate = n_autos / n_corrs

    assert annot_retention >= prev_annot_retention
    assert edit_retention >= prev_edit_retention
    assert auto_rate >= prev_auto_rate

def evaluate_no_benchmark(run_id, run):
    failed, log_exists, n_annots, n_edits, n_corrs = floatify(
        run['failed'],
        run['log_exists'],
        run['n_annots'],
        run['n_edits'],
        run['n_corrs'],
    )
    assert failed == 0
    assert log_exists == 1
    assert n_edits / n_annots >= ANNOT_RETENTION
    assert n_corrs / n_edits >= EDIT_RETENTION

def update_benchmark():
    latest_path = get_latest_path()
    command = ['cp', str(latest_path), str(BENCHMARK_PATH)]
    subprocess.run(command, check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--update-benchmark',
        action='store_true',
    )
    args = parser.parse_args()
    if args.update_benchmark:
        update_benchmark()
        return
    test_regressions()    
    
if __name__ == '__main__':
    main()
    
