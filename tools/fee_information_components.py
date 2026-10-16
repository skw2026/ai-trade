"""Explain frozen predictions; no return-model fit, strategy or promotion API."""
import math

from information_attribution import DAY, date_hash, hac7_se, mean, require, solve


def close(a, b):
    require(math.isfinite(a) and math.isfinite(b) and
            math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-7), 'IDENTITY_MISMATCH')


def normalized(row, model):
    require(len(row['x']) == 4 and all(math.isfinite(v) for v in row['x']), 'INPUT_SHAPE_FINITE')
    return [(v-mu)/sd if sd > 1e-12 else 0.
            for v, mu, sd in zip(row['x'], model['center'], model['scale'])]


def decompose(artifact):
    """Input-only projection uses training X/z, never training target y."""
    train, evaluation = artifact['train'], artifact['evaluation']
    original = artifact['result']
    base, aug = original['models']['price'], original['models']['increment']
    require(bool(train) and bool(evaluation), 'EMPTY_INPUT')
    require(base['mode'] == 'price' and aug['mode'] == 'increment', 'MODEL_MODE')
    for model, width in ((base, 3), (aug, 4)):
        for key in ('center', 'scale', 'coefficients'):
            require(len(model[key]) == width and all(math.isfinite(v) for v in model[key]), 'MODEL_SHAPE_FINITE')
        require(all(v >= 0 for v in model['scale']), 'NEGATIVE_SCALE')
        require(model['training_rows'] == len(train), 'TRAINING_COUNT')
    require(base['center'] == aug['center'][:3] and base['scale'] == aug['scale'][:3]
            and base['intercept'] == aug['intercept'] and base['ridge'] == aug['ridge'] == .1,
            'FROZEN_MODEL_CONTRACT')
    require(len({r['t'] for r in train}) == len(train) and
            len({r['t'] for r in evaluation}) == len(evaluation), 'DUPLICATE_DATE')
    require(all(r['t'] % DAY == 0 and r['label_end'] == r['t']+DAY for r in train+evaluation), 'DAILY_CLOCK')
    require(max(r['label_end'] for r in train) <= min(r['t'] for r in evaluation), 'TRAIN_EVAL_OVERLAP')
    require(date_hash(train) == original['train_date_sha256'], 'TRAIN_DATE_IDENTITY')
    n = [normalized(r, aug) for r in train]
    gram = [[mean([r[i]*r[j] for r in n]) for j in range(3)] for i in range(3)]
    cross = [mean([r[j]*r[3] for r in n]) for j in range(3)]
    projection = solve(gram, cross)  # Singular -> fail; no extra ridge or fit fallback.
    bz = aug['coefficients'][3]
    shift = [a-b for a, b in zip(aug['coefficients'][:3], base['coefficients'])]
    for j in range(3):
        close(sum((gram[j][k]+(.1 if j == k else 0))*shift[k] for k in range(3)), -cross[j]*bz)
    residual = [r[3]-math.fsum(r[j]*projection[j] for j in range(3)) for r in n]
    for j in range(3):
        close(mean([r[j]*v for r, v in zip(n, residual)]), 0.)
    predictions = original['predictions']
    require(len(predictions) == len(evaluation), 'PREDICTION_COUNT')
    rows = []
    for row, pred in zip(evaluation, predictions):
        require((row['t'], row['label_end'], row['y']) == (pred['t'], pred['label_end'], pred['y']), 'PREDICTION_IDENTITY')
        x = normalized(row, aug)
        close(pred['price'], base['intercept']+math.fsum(b*v for b, v in zip(base['coefficients'], x)))
        close(pred['increment'], aug['intercept']+math.fsum(b*v for b, v in zip(aug['coefficients'], x)))
        u = bz*(x[3]-math.fsum(x[j]*projection[j] for j in range(3)))
        v = math.fsum(x[j]*(shift[j]+bz*projection[j]) for j in range(3))
        delta = pred['increment']-pred['price']
        close(u+v, delta)
        error = pred['y']-pred['price']
        gain_u, gain_v = 2*error*u-u*u-u*v, 2*error*v-v*v-u*v
        total = error**2-(pred['y']-pred['increment'])**2
        close(gain_u+gain_v, total)
        rows.append(dict(t=row['t'], label_end=row['label_end'],
            y=pred['y'], price=pred['price'], increment=pred['increment'],
            u=u, v=v, delta=delta, total_gain=total, residual_gain=gain_u, price_gain=gain_v))
    return dict(projection=projection, training_rows=len(train),
        training_residual_variance=mean([v*v for v in residual]),
        signal_coefficient_bps=bz, rows=rows)


def summarize(rows, start, end):
    require(start < end and start % DAY == 0 and end % DAY == 0, 'STAGE_CLOCK')
    selected = [r for r in rows if start <= r['t'] < end and r['label_end'] <= end]
    require(len(selected) > 28 and len({r['t'] for r in selected}) == len(selected), 'STAGE_CAPACITY')
    groups = {}
    for r in selected:
        groups.setdefault((r['t']-start)//(28*DAY), []).append(r)
    require(len(groups) >= 2, 'BLOCK_CAPACITY')
    components = {}
    for key in ('total_gain', 'residual_gain', 'price_gain'):
        deleted = []
        for index, group in sorted(groups.items()):
            retained = [r[key] for r in selected if (r['t']-start)//(28*DAY) != index]
            deleted.append(dict(block=index, start=start+index*28*DAY,
                end=min(end, start+(index+1)*28*DAY), removed_rows=len(group),
                retained_rows=len(retained), retained_mean_bps_squared=mean(retained)))
        estimate = mean([r[key] for r in selected])
        values = [r['retained_mean_bps_squared'] for r in deleted]
        components[key] = dict(mean_bps_squared=estimate,
            hac7_se_bps_squared=hac7_se([(r['t'], r[key]) for r in selected]),
            leave_block_out=deleted, minimum=min(values), maximum=max(values),
            opposite_sign_blocks=sum(v*estimate < 0 for v in values))
    scale = {}
    for key in ('increment', 'u', 'delta'):
        values = [abs(r[key]) for r in selected]
        scale[key] = dict(mean_abs_bps=mean(values), max_abs_bps=max(values),
            above_15bps=sum(v > 15 for v in values), above_30bps=sum(v > 30 for v in values))
    return dict(rows=len(selected), date_sha256=date_hash(selected), components=components,
        forecast_scale=scale, cost_status='COST_HEADROOM_NOT_ESTABLISHED',
        independent_confirmation=False, causal_identification=False, profit_estimate=None,
        promotion_eligible=False, g4=False, trading_paths_executed=0)
