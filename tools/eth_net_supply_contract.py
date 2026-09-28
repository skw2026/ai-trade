#!/usr/bin/env python3
"""Offline reference semantics for a DESIGN_ONLY contract; no fetch/return engine."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'docs/contracts/eth_net_supply_proxy_v1.json'
RUN = ROOT / '.artifacts/eth-net-supply-design-20260928'
DAY = dt.timedelta(days=1)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def strict(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'DUPLICATE_KEY')
            value[key] = item
        return value
    def constant(_):
        raise ValueError('NONFINITE_JSON')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def instant(value):
    require(isinstance(value, str) and value.endswith('Z'), 'UTC_Z_REQUIRED')
    result = dt.datetime.fromisoformat(value[:-1] + '+00:00')
    require(result.utcoffset() == dt.timedelta(0), 'UTC_REQUIRED')
    return result


def stamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def positive(value):
    require(isinstance(value, str) and bool(value.strip()), 'DECIMAL_STRING_REQUIRED')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('INVALID_DECIMAL') from exc
    require(number.is_finite() and number > 0, 'POSITIVE_FINITE_REQUIRED')
    return number


def contract():
    return strict(CONTRACT.read_bytes())


def calendar(c):
    first, last = instant(c['calendar']['first_entry']), instant(c['calendar']['last_entry'])
    require(c['calendar']['spacing_days'] == c['calendar']['holding_days'] == 7, 'WEEKLY_ONLY')
    require(first <= last, 'CALENDAR_ORDER')
    entries = []
    while first <= last:
        entries.append(stamp(first))
        first += 7 * DAY
    return entries


def observe(c, entry, rows, prices, retrieved_at):
    """Return one clock-qualified *reconstructed* observation, never a PIT proof.

    rows: normalized asset/frequency/day/supply/completion strings, plus optional
    known_revision bool. prices: exactly the two prescribed timestamp/value pairs.
    Missing input is a predeclared exclusion; malformed input raises a hard stop.
    This helper is exercised only with synthetic input in the design stage.
    """
    require(entry in calendar(c), 'OFF_CALENDAR_ENTRY')
    decision, retrieved = instant(entry), instant(retrieved_at)
    require(retrieved >= decision + c['calendar']['holding_days'] * DAY,
            'RECONSTRUCTED_SCREEN_RETRIEVAL_BEFORE_HORIZON')
    midnight = decision.replace(hour=0, minute=0, second=0, microsecond=0)
    old = midnight + c['supply']['old_day_offset'] * DAY
    new = midnight + c['supply']['new_day_offset'] * DAY
    expected = [(old + i * DAY).date().isoformat() for i in range(8)]
    require(new - old == 7 * DAY and c['supply']['required_consecutive_days'] == 8,
            'FIXED_EIGHT_DAY_WINDOW')
    require(decision - (new + DAY) >= dt.timedelta(hours=36), 'MINIMUM_LAG')
    indexed = {}
    for row in rows:
        require(row.get('asset') == 'eth' and row.get('frequency') == '1d', 'SUPPLY_IDENTITY')
        day = row['day']
        require(isinstance(day, str) and dt.date.fromisoformat(day).isoformat() == day,
                'DAY_FORMAT')
        require(day in expected and day not in indexed, 'DUPLICATE_OR_OFF_WINDOW')
        require(type(row.get('known_revision', False)) is bool, 'REVISION_FLAG_TYPE')
        require(not row.get('known_revision', False), 'KNOWN_REVISION_REQUIRES_REVIEW')
        indexed[day] = row
    base = dict(entry=entry, scope='RECONSTRUCTED_SNAPSHOT_ONLY',
                historical_vintage_proven=False, candidate_qualified=False)
    reasons, supplies, clocks = set(), {}, []
    for day in expected:
        row = indexed.get(day)
        if row is None:
            reasons.add('MISSING_DAY')
            continue
        if row.get('SplyCur') is None:
            reasons.add('MISSING_SUPPLY')
        else:
            supplies[day] = positive(row['SplyCur'])
        period_end = instant(day + 'T00:00:00Z') + DAY
        for field in c['supply']['completion_fields']:
            value = row.get(field)
            if value is None:
                reasons.add('MISSING_COMPLETION')
                continue
            require(isinstance(value, str) and value.isascii() and value.isdigit(),
                    'COMPLETION_INTEGER_STRING')
            clock = dt.datetime.fromtimestamp(int(value), dt.timezone.utc)
            require(period_end <= clock <= retrieved, 'COMPLETION_OUT_OF_RANGE')
            clocks.append(clock)
            if clock > decision:
                reasons.add('COMPLETION_AFTER_DECISION')
    old_price_time, new_price_time = stamp(old + DAY), stamp(new + DAY)
    require(set(prices) == {old_price_time, new_price_time}, 'FIXED_PRICE_TIMESTAMPS')
    old_price, new_price = positive(prices[old_price_time]), positive(prices[new_price_time])
    if new_price == old_price:
        reasons.add('PRICE_TIE')
    if len(supplies) == 8 and supplies[expected[-1]] == supplies[expected[0]]:
        reasons.add('SUPPLY_TIE')
    if reasons:
        return dict(base, valid=False, reasons=sorted(reasons))
    return dict(base, valid=True, reasons=[],
                supply_direction=1 if supplies[expected[-1]] < supplies[expected[0]] else -1,
                price_direction=1 if new_price > old_price else -1,
                recorded_completion_max=stamp(max(clocks)))


def capacity(c, observations):
    entries = calendar(c)
    require([o['entry'] for o in observations] == entries, 'ALL_CALENDAR_SLOTS_ONCE_IN_ORDER')
    cells = {f'{s},{p}': 0 for s in (-1, 1) for p in (-1, 1)}
    halves = [0, 0]
    valid = 0
    for o in observations:
        require(type(o['valid']) is bool and isinstance(o['reasons'], list), 'OBSERVATION_SCHEMA')
        require(o['scope'] == 'RECONSTRUCTED_SNAPSHOT_ONLY' and
                o['historical_vintage_proven'] is False and o['candidate_qualified'] is False,
                'NO_QUALIFICATION_UPGRADE')
        require(bool(o['reasons']) is (not o['valid']), 'EXCLUSION_REASON_REQUIRED')
        if not o['valid']:
            continue
        s, p = o['supply_direction'], o['price_direction']
        require(type(s) is int and type(p) is int and s in (-1, 1) and p in (-1, 1),
                'DIRECTION_REQUIRED')
        cells[f'{s},{p}'] += 1
        halves[int(instant(o['entry']) >= instant(c['calendar']['split']))] += 1
        valid += 1
    limits = c['capacity']
    passed = (valid >= limits['minimum_valid_weeks'] and
              min(cells.values()) >= limits['minimum_each_supply_price_sign_cell'] and
              min(halves) >= limits['minimum_each_calendar_half'])
    return dict(status='CAPACITY_PASS_ONLY' if passed else 'CAPACITY_INSUFFICIENT',
                valid=valid, excluded=len(entries)-valid, cells=cells, halves=halves,
                economic_effect='NOT_EVALUATED', experiment_authorized=False,
                candidate_qualified=False)


def audit_design():
    c = contract()
    require(c['stage'] == 'DESIGN_ONLY' and all(v is False for v in c['authority'].values()),
            'DESIGN_AUTHORITY')
    require(c['candidate_qualified'] is False and c['availability']['historical_vintage_proven'] is False,
            'NO_CANDIDATE_OR_PIT')
    entries = calendar(c)
    require(len(entries) == c['calendar']['expected_slots'] == 157, 'CALENDAR_COUNT')
    baseline = strict((RUN / 'baseline.json').read_bytes())
    require(all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h
                for p, h in baseline['preserved_sha256'].items()), 'PRESERVED_EVIDENCE_CHANGED')
    sources = ['docs/plans/2026-09-28-eth-net-supply-design.md',
               str(CONTRACT.relative_to(ROOT)), 'tools/eth_net_supply_contract.py',
               'tools/test_eth_net_supply_contract.py']
    return dict(status='DESIGN_VALIDATED_ONLY', calendar_slots=len(entries),
                historical_capacity='NOT_EVALUATED', economic_effect='NOT_EVALUATED',
                preserved_files=len(baseline['preserved_sha256']), preserved_gates=14,
                new_research_reads=0, economic_attempts=0, candidate_qualified=False,
                source_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources})


if __name__ == '__main__':
    print(json.dumps(audit_design(), indent=2))
