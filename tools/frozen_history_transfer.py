"""A fixed historical research path, no fitting, activation or risk reset."""
import math
from offline_policy_correction import Path
from offline_diagnostic_safety import LossDiagnostic
from run_bounded_learning import need, hac_lower

MODE='FROZEN_RESEARCH_DIAGNOSTIC_ONLY'
VECTORS=((0,-4),(1,0))
GAP=7*86400000


class FrozenDiagnostic(LossDiagnostic):
    def assess(self):
        result=super().assess()
        result['mode']=MODE
        return result


class FrozenPath(Path):
    def __init__(self,coefficients,*,domain='frozen_historical_transfer'):
        need(domain=='frozen_historical_transfer','FROZEN_RESEARCH_ONLY')
        need(tuple(coefficients) in VECTORS,'FIXED_VECTORS_ONLY')
        super().__init__(coefficients)
        self.safety=FrozenDiagnostic()
        self.first_legacy_withdrawal=None

    def consume(self,sample):
        event=super().consume(sample)
        if event and event.get('safety',{}):
            if event['safety']['would_withdraw'] and self.first_legacy_withdrawal is None:
                self.first_legacy_withdrawal=event['timestamp']+300000
        return event

    def finish(self):
        result=super().finish()
        legacy=('REJECT_LEGACY_SAFETY' if self.first_legacy_withdrawal is not None else
                'REJECT_REFERENCE_RISK' if self.reason!='COMPLETE' else
                'PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION')
        result.update(safety_mode=MODE,first_legacy_withdrawal_ms=self.first_legacy_withdrawal,
            diagnostic_triggered_windows=self.safety.triggered_windows,
            legacy_path_status=legacy,qualification=False)
        return result


def replay(samples,coefficients,emit=None):
    path=FrozenPath(coefficients)
    for sample in samples:
        event=path.consume(sample)
        if emit and event is not None:emit(event)
    result=path.finish()
    if emit:emit(dict(kind='TERMINAL',result=result))
    return result


def concentration(events,summary):
    """Merge direct reversals into a continuous exposure episode (conservative)."""
    episodes=[];opened=None;previous=[dict(qty=0,cash=10000,fills=0)]*2
    for e in events:
        terminal=e['kind']=='TERMINAL'
        books=e['result']['wallets'] if terminal else e['wallets']
        at=e['result']['exit_at_ms'] if terminal else e['timestamp']
        was,now=previous[0]['qty'],books[0]['qty']
        if not was and now:
            need(opened is None,'EPISODE_ALREADY_OPEN')
            opened=dict(start_ms=at,before=[b['cash'] for b in previous],fills=[b['fills'] for b in previous],bars=0)
        if now:
            need(opened is not None,'EPISODE_MISSING')
            opened['bars']+=1
        if was and not now:
            need(opened is not None,'EPISODE_MISSING')
            episodes.append(dict(start_ms=opened['start_ms'],end_ms=at,
                net=[b['cash']-v for b,v in zip(books,opened['before'])],
                fills=[b['fills']-v for b,v in zip(books,opened['fills'])],bars=opened['bars']))
            opened=None
        previous=books
    need(opened is None,'UNCLOSED_EPISODE')
    for k in (0,1):
        need(abs(sum(e['net'][k] for e in episodes)-(summary['wallets'][k]['cash']-10000))<1e-6,'EPISODE_NET_SUM')
        need(sum(e['fills'][k] for e in episodes)==summary['wallets'][k]['fills'],'EPISODE_FILL_SUM')
    need(sum(e['bars'] for e in episodes)==sum(summary['exposure_short_flat_long'][::2]),'EPISODE_BAR_SUM')
    clusters=[]
    for e in episodes:
        if not clusters or e['start_ms']-clusters[-1]['end_ms']>=GAP:
            clusters.append(dict(start_ms=e['start_ms'],end_ms=e['end_ms'],net=[0.,0.],episodes=0))
        cluster=clusters[-1]
        cluster['end_ms']=e['end_ms'];cluster['episodes']+=1
        cluster['net']=[a+b for a,b in zip(cluster['net'],e['net'])]
    residual=[sum(c['net'][k] for c in clusters)-max((c['net'][k] for c in clusters),default=0) for k in (0,1)]
    return dict(episodes=episodes,episode_count=len(episodes),clusters=clusters,
        separated_clusters=len(clusters),leave_largest_cluster_net=residual,
        nonzero_full_blocks=sum(any(x!=0 for x in w) for w in summary['weekly']))


def verdict(learned,fixed,clusters):
    need(len(learned['weekly'])==len(fixed['weekly']),'PAIRED_BLOCK_COUNT')
    nets=[b['cash']-10000 for b in learned['wallets']]
    paired=[a['cash']-b['cash'] for a,b in zip(learned['wallets'],fixed['wallets'])]
    if learned['reason']!='COMPLETE':return 'REJECT_REFERENCE_RISK',None
    if min(nets+paired)<=0:return 'REJECT_NET_EDGE',None
    if clusters['separated_clusters']<5:return 'INSUFFICIENT_EVENT_CAPACITY',None
    if min(clusters['leave_largest_cluster_net'])<=0:return 'INSUFFICIENT_CONCENTRATION',None
    bounds=dict(learned_stress=hac_lower([w[1] for w in learned['weekly']]),
        paired_base=hac_lower([a[0]-b[0] for a,b in zip(learned['weekly'],fixed['weekly'])]),
        paired_stress=hac_lower([a[1]-b[1] for a,b in zip(learned['weekly'],fixed['weekly'])]))
    if not all(v is not None and math.isfinite(v) and v>0 for v in bounds.values()):
        return 'INSUFFICIENT_STATISTICAL_SUPPORT',bounds
    return 'HISTORICAL_TRANSFER_SUPPORTED_NOT_DEPLOYABLE',bounds
