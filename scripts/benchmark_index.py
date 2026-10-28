"""Read-only performance comparison with the original scanning algorithm.

The selected data directory is never written to. SQLite caches and reports go
only to the explicitly supplied work directory. OS disk caches are not flushed.
"""

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.index_service import DirectoryScanner, IndexService


def original_scan(root):
    groups = defaultdict(lambda: defaultdict(lambda: {'size': 0, 'paths': []}))
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    metadata = entry.stat()
                    earliest = min(metadata.st_ctime, metadata.st_mtime, metadata.st_atime)
                    stamp = time.localtime(earliest)
                    group = groups[stamp.tm_year][stamp.tm_mon]
                    group['size'] += metadata.st_size
                    group['paths'].append(entry.path)
    months = {(year, month): (len(group['paths']), group['size'])
              for year, values in groups.items() for month, group in values.items()}
    return months


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    baseline = original_scan(args.root)
    baseline_time = time.perf_counter() - started
    print(json.dumps({'phase': 'original_scan', 'seconds': baseline_time,
                      'files': sum(value[0] for value in baseline.values())}), flush=True)
    scanner = DirectoryScanner()
    started = time.perf_counter()
    native_count = 0
    for listing, error in scanner.walk(str(args.root.resolve())):
        if error:
            raise OSError(error)
        native_count += len(listing.files)
    native_time = time.perf_counter() - started
    print(json.dumps({'phase': 'native_enumeration', 'seconds': native_time,
                      'files': native_count, 'backend': scanner.backend}), flush=True)
    service = IndexService(args.root, args.work_dir / 'indexes')
    result = service.refresh(force=True)
    summary = service.store.summary()
    indexed = {(row['year'], row['month']): (row['file_count'], row['total_size'])
               for row in summary['months']}
    if indexed != baseline:
        raise AssertionError('Indexed month/count/size totals differ from the original scan.')
    print(json.dumps({'phase': 'first_index_build', 'seconds': result.elapsed,
                      'files': result.files}), flush=True)
    cached_samples = []
    for _ in range(25):
        started = time.perf_counter()
        service.store.summary()
        cached_samples.append(time.perf_counter() - started)
    refresh = service.refresh()
    report = {
        'root': str(args.root.resolve()), 'files': result.files,
        'directories': result.directories, 'total_size': summary['total_size'],
        'baseline_seconds': baseline_time, 'native_enumeration_seconds': native_time,
        'first_build_seconds': result.elapsed,
        'cached_summary_median_ms': statistics.median(cached_samples) * 1000,
        'unchanged_incremental_seconds': refresh.elapsed,
        'unchanged_incremental_mode': refresh.mode, 'unchanged_records_written': refresh.changed,
        'backend': result.backend, 'month_count_size_match': True,
        'sqlite_integrity': service.store.integrity_check(),
        'note': 'Read-only source; repeated timings use normal Windows disk caches. No Everything comparison was run.',
    }
    service.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
