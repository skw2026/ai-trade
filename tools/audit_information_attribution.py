"""Decimal implementation independent of the diagnostic fitter and scorer."""
from decimal import Decimal as D, localcontext
import hashlib

DAY = 86400000


def close(actual, expected, label):
    if abs(D(str(actual))-expected) > D('0.00000002')*max(D(1),abs(expected)):
        raise ValueError('INFORMATION_AUDIT_'+label)


def independent_design(row, mode):
    values = [D(str(v)) for v in row['x'][:3]]
    if mode != 'price':
        key = 'information-attribution-v1:%d' % (row['t']//604800000)
        sign = 1 if mode == 'increment' or hashlib.sha256(key.encode()).digest()[0] % 2 else -1
        values.append(D(str(row['x'][3]))*sign)
    return values


def regression(training, evaluation, models, predictions):
    with localcontext() as ctx:
        ctx.prec = 40
        for mode, model in models.items():
            x = [independent_design(r,mode) for r in training]; n = D(len(x)); k = len(x[0])
            y = [D(str(r['y'])) for r in training]
            center = [sum(v[j] for v in x)/n for j in range(k)]
            scale = [(sum((v[j]-center[j])**2 for v in x)/n).sqrt() for j in range(k)]
            z = [[(v-m)/s if s>D('1e-12') else D(0) for v,m,s in zip(row,center,scale)] for row in x]
            intercept = sum(y)/n
            a = [[sum(v[i]*v[j] for v in z)/n+(D('.1') if i==j else D(0)) for j in range(k)]
                 +[sum(v[i]*(r-intercept) for v,r in zip(z,y))/n] for i in range(k)]
            # Forward elimination and back substitution, unlike production Gauss-Jordan.
            for j in range(k):
                p = max(range(j,k),key=lambda i:abs(a[i][j])); a[j],a[p]=a[p],a[j]
                for i in range(j+1,k):
                    factor=a[i][j]/a[j][j]
                    for c in range(j,k+1): a[i][c]-=factor*a[j][c]
            beta=[D(0)]*k
            for i in reversed(range(k)):
                beta[i]=(a[i][-1]-sum(a[i][j]*beta[j] for j in range(i+1,k)))/a[i][i]
            close(model['intercept'],intercept,'INTERCEPT')
            for name, values in (('center',center),('scale',scale),('coefficients',beta)):
                for actual,expected in zip(model[name],values): close(actual,expected,name)
            for row,p in zip(evaluation,predictions):
                if (p['t'],p['label_end'],p['y']) != (row['t'],row['label_end'],row['y']):
                    raise ValueError('INFORMATION_AUDIT_PREDICTION_IDENTITY')
                expected=intercept+sum(b*(v-m)/s if s>D('1e-12') else D(0)
                                      for b,v,m,s in zip(beta,independent_design(row,mode),center,scale))
                close(p[mode],expected,'PREDICTION')
    return dict(status='DECIMAL_REGRESSION_VERIFIED', fits=len(models), predictions=len(predictions)*len(models))


def samples(rows, closes, horizon):
    with localcontext() as ctx:
        ctx.prec=40
        for row in rows:
            t=row['t']
            if row['label_end'] != t+horizon*DAY: raise ValueError('INFORMATION_AUDIT_LABEL_CLOCK')
            returns=[(D(str(closes[t-i*DAY]))/D(str(closes[t-(i+1)*DAY]))).ln() for i in range(7)]
            mu=sum(returns)/7
            values=[returns[0]*10000,sum(returns)*10000,(sum((v-mu)**2 for v in returns)/7).sqrt()*10000]
            for actual,expected in zip(row['x'][:3],values): close(actual,expected,'PRICE_FEATURE')
            expected=(D(str(closes[row['label_end']]))/D(str(closes[t]))).ln()*10000
            close(row['y'],expected,'LABEL')
    return dict(status='DECIMAL_PRICE_AND_LABEL_VERIFIED',rows=len(rows))


def metrics(predictions, report):
    if not predictions:
        if report != dict(status='INSUFFICIENT_MATCHED_ROWS',rows=0): raise ValueError('INFORMATION_AUDIT_EMPTY')
        return
    n=D(len(predictions)); losses={}
    for mode in ('price','increment','placebo'):
        losses[mode]=[(D(str(r['y']))-D(str(r[mode])))**2 for r in predictions]
        close(report['mse_bps_squared'][mode],sum(losses[mode])/n,'MSE')
        close(report['mae_bps'][mode],sum(abs(D(str(r['y']))-D(str(r[mode]))) for r in predictions)/n,'MAE')
    delta=[a-b for a,b in zip(losses['price'],losses['increment'])]; mu=sum(delta)/n
    close(report['increment_mse_reduction_bps_squared'],mu,'DELTA')
    base=sum(losses['price'])/n
    for key,v in (('increment_mse_reduction_percent',mu),
                  ('placebo_mse_reduction_percent',base-sum(losses['placebo'])/n)):
        if base: close(report[key],100*v/base,'PERCENT')
        elif report[key] is not None: raise ValueError('INFORMATION_AUDIT_ZERO_MSE')
    residuals={r['t']:v-mu for r,v in zip(predictions,delta)}
    variance=sum(v*v for v in residuals.values())
    for lag in range(1,8):
        variance+=2*(1-D(lag)/8)*sum(v*residuals[t-lag*DAY] for t,v in residuals.items() if t-lag*DAY in residuals)
    if len(predictions)>1: close(report['paired_loss_difference_hac7_se'],max(D(0),variance).sqrt()/n,'HAC')
    identity=','.join('%d:%d'%(r['t'],r['label_end']) for r in predictions)
    if report['date_sha256']!=hashlib.sha256(identity.encode()).hexdigest(): raise ValueError('INFORMATION_AUDIT_DATES')
    if report['p_value'] is not None or report['profit_estimate'] is not None or report['independent_samples']:
        raise ValueError('INFORMATION_AUDIT_SCOPE')


def classification(stages, actual):
    if any(s['status']=='INSUFFICIENT_MATCHED_ROWS' for s in stages): expected='INSUFFICIENT_MATCHED_ROWS'
    elif all(s['mse_bps_squared']['increment'] < min(s['mse_bps_squared']['price'],s['mse_bps_squared']['placebo'])
             for s in stages): expected='DESCRIPTIVE_INCREMENT_ONLY'
    else: expected='LINEAR_INCREMENT_NOT_STABLE'
    if actual!=expected: raise ValueError('INFORMATION_AUDIT_CLASSIFICATION')
