"""BTC address breadth development input. No network, account or promotion API.

This snapshot is reconstructed, not historical point-in-time certification.
Shared execution/selection rules are reused without replaying old mechanisms.
"""
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import io
import math

import btc_old_output_model as execution
from btc_fee_pressure import (FIXED, NAMES, complete, grids, select,
                              specifications, training_decision, verdict)

METRIC = 'AdrActCnt'
SECOND_METRIC = 'TxTfrValAdjNtv'


def require(ok, reason):
    if not ok:
        raise ValueError('ADDRESS_' + reason)


def required_dates():
    out = []
    for left, right in (('2020-12-01', '2021-12-30'), ('2024-12-01', '2026-01-01')):
        a, b = dt.date.fromisoformat(left), dt.date.fromisoformat(right)
        out.extend(str(a + dt.timedelta(days=i)) for i in range((b-a).days))
    return out


def inspect_input(raw, dates=None):
    dates = required_dates() if dates is None else list(dates)
    require(len(dates) == len(set(dates)) and dates, 'REQUIRED_GRID')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
    fields = reader.fieldnames
    require(fields and len(set(fields)) == len(fields) and 'time' in fields and
            METRIC in fields, 'SCHEMA')
    all_rows = {}; previous = None
    for row in reader:
        require(None not in row and all(v is not None for v in row.values()), 'ROW_WIDTH')
        d = dt.date.fromisoformat(row['time'])
        require(str(d) == row['time'] and (previous is None or d > previous), 'DATE_ORDER')
        previous = d; all_rows[str(d)] = row
    values = {}; completion_late = []; missing_completion = []; completion_times = {}
    for date in dates:
        require(date in all_rows, 'MISSING_DAY:' + date)
        row = all_rows[date]
        try:
            value = Decimal(row[METRIC])
        except InvalidOperation as e:
            raise ValueError('ADDRESS_VALUE:' + date) from e
        require(value.is_finite() and value > 0, 'VALUE:' + date)
        require(value == value.to_integral_value(), 'INTEGER:' + date)
        values[date] = str(value)
        raw_completion = row.get('AssetEODCompletionTime', '')
        if not raw_completion:
            missing_completion.append(date)
            completion_times[date] = None
        else:
            try:
                completion = Decimal(raw_completion)
            except InvalidOperation as e:
                raise ValueError('ADDRESS_COMPLETION') from e
            require(completion.is_finite() and completion > 0 and
                    completion == completion.to_integral_value(), 'COMPLETION')
            assumed = execution.millis(date)//1000 + 3*86400
            if completion > assumed:
                completion_late.append(date)
            other = row.get('AssetCompletionTime', '')
            if other:
                try:
                    other = Decimal(other)
                except InvalidOperation as e:
                    raise ValueError('ADDRESS_COMPLETION') from e
                require(other.is_finite() and other > 0 and other == other.to_integral_value(), 'COMPLETION')
                completion = max(completion, other)
            completion_times[date] = int(completion)*1000
    second_present = SECOND_METRIC in fields
    return dict(status='RECONSTRUCTED_DEVELOPMENT_INPUT_ONLY', metric=METRIC,
                total_rows=len(all_rows), required_days=len(dates), values=values,
                completion_times=completion_times,
                point_in_time_certified=False, missing_completion_days=missing_completion,
                completion_after_assumed_clock_days=completion_late,
                second_metric=SECOND_METRIC,
                second_input_status='PRESENT_NOT_ADMITTED' if second_present else 'MISSING_COLUMN',
                selected_mechanism='ACTIVE_ADDRESS_PARTICIPATION',
                selection_basis='PREREGISTERED_INPUT_PRIORITY_NOT_RETURNS')


def log_values(admitted):
    require(admitted['status'] == 'RECONSTRUCTED_DEVELOPMENT_INPUT_ONLY' and
            admitted['metric'] == METRIC, 'ADMISSION_IDENTITY')
    return {d: math.log(float(v)) for d, v in admitted['values'].items()}


def features(rows, logs, start, end, lag_days=3, *, completion_times):
    require(lag_days in (3, 4), 'FIXED_LAG')
    out = execution.features(rows, logs, start, end, lag_days=lag_days)
    for t, f in out.items():
        day = dt.date.fromisoformat(f['source_day'])
        dates = [str(day-dt.timedelta(days=i)) for i in range(29)]
        times = [completion_times.get(d) for d in dates]
        ready = all(v is not None and v <= t for v in times)
        past = [logs[d] for d in dates[1:]]
        mu = math.fsum(past)/28
        sd = math.sqrt(math.fsum((v-mu)**2 for v in past)/28)
        weekday_mean = math.fsum(logs[dates[i]] for i in (7,14,21,28))/4
        f['z'] = execution.clip((logs[dates[0]]-weekday_mean)/sd) if ready and sd>=1e-6 else None
        f['completion_ready'] = ready
        f['latest_completion_ms'] = max(times) if all(v is not None for v in times) else None
    return out
