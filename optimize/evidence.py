"""Bounded, observational optimization evidence. Never used for scoring."""
import json
from pathlib import Path

from quant.run.artifacts import equity_curve_with_timestamps, _dataframe_records
from quant.run.metrics import _fills_report
from quant.run.backtest_common import VENUE


def sample_points(points, limit=500):
    if len(points) <= limit:
        return points
    return [points[i * (len(points) - 1) // (limit - 1)] for i in range(limit)]


def capture(engine, start_ns, end_ns):
    import pandas as pd
    points = [p for p in equity_curve_with_timestamps(engine, VENUE)
              if start_ns <= pd.Timestamp(p['ts']).value <= end_ns]
    points.sort(key=lambda p: pd.Timestamp(p['ts']).value)
    fills = _fills_report(engine)
    if fills is not None and not fills.empty:
        if 'ts_last' in fills.columns:
            times = pd.to_datetime(fills['ts_last'], utc=True, errors='coerce').astype('datetime64[ns, UTC]').astype('int64')
            fills = fills.assign(evidence_time_ns=times)
            fills = fills[(times >= start_ns) & (times <= end_ns)].sort_values('evidence_time_ns')
        elif 'ts_event' in fills.columns:
            times = pd.to_numeric(fills['ts_event'], errors='coerce')
            fills = fills[(times >= start_ns) & (times <= end_ns)].sort_values('ts_event')
        else:
            fills = fills.iloc[:0]
    return {'equity': sample_points(points), 'equity_total': len(points),
            'equity_sampled': len(points) > 500,
            'fills': _dataframe_records(fills, 200),
            'benchmark': {'status': 'unavailable', 'message': 'No aligned benchmark captured.'}}


class EvidenceWriter:
    def __init__(self, progress_path):
        self.directory = Path(progress_path).with_suffix('.evidence') if progress_path else None

    def write(self, trial, payload):
        if self.directory is None:
            return False
        temporary = self.directory / f'{trial}.tmp'
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
            temporary.replace(self.directory / f'{trial}.json')
            summary = {k: v for k, v in payload.items() if k not in {'folds', 'effective_params'}}
            summary['folds_complete'] = len(payload.get('folds', []))
            temporary.write_text(json.dumps(summary, allow_nan=False), encoding='utf-8')
            temporary.replace(self.directory / f'{trial}.summary.json')
            return True
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
