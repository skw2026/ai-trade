import copy
import json
import unittest
from decimal import Decimal as D
from analyze_bybit_linear_observability import analyze,parse,transition,sizes,EXPIRIES,SYMBOLS

START=100000


def frame(symbol='BTCUSDT',mono=100,ts=None,cts=None,u=10,seq=10,bid='100',ask='101',qty='100'):
    ts=START+mono-5 if ts is None else ts
    cts=ts-5 if cts is None else cts
    return dict(kind='message',source='spot' if symbol=='BTCUSDT' else 'linear',
        mono_ms=mono,wall_ms=START+mono,payload=dict(raw=json.dumps(dict(
        topic='orderbook.1.'+symbol,type='snapshot',ts=ts,cts=cts,
        data=dict(s=symbol,u=u,seq=seq,b=[[bid,qty]],a=[[ask,qty]])))))


def controls():
    return [dict(kind='message',source=s,mono_ms=5,wall_ms=START+5,
                 payload=dict(raw=json.dumps(dict(op='subscribe',success=True)))) for s in ('spot','linear')]


def receipt(stop=120000):
    return dict(start_wall_ms=START,fixed_duration_ms=120000,reconnects=0,
                observation_stop_ms=stop,status='CAPTURE_COMPLETE' if stop==120000 else 'INCOMPLETE')


class ObservabilityTests(unittest.TestCase):
    def test_full_denominator_with_nothing(self):
        r=analyze([],receipt())
        self.assertEqual(len(r['pairs']),960)
        self.assertEqual(r['recent_l1_pairs'],0)
        self.assertEqual(r['term_summary'][0]['missing_or_anomalous'],120)

    def test_no_future_frame_backfill(self):
        rows=controls()+[frame(mono=1011),frame(EXPIRIES[0],mono=1011)]
        r=analyze(rows,receipt())
        self.assertIn('MISSING_SNAPSHOT',r['pairs'][0]['reasons'])
        self.assertTrue(r['pairs'][8]['legacy_condition'])

    def test_same_u_republication_old_engine_clock(self):
        rows=controls()+[frame(),frame(EXPIRIES[0]),frame(mono=3100,cts=100090)]
        r=analyze(rows,receipt())
        self.assertEqual(r['old_clock_republication_evidence'],1)
        self.assertEqual(r['hypothesis'],'OBSERVED_OLD_CTS_REPUBLICATION')
        self.assertEqual(r['economic_computations'],0)

    def test_republication_with_refreshed_cts_separate(self):
        r=analyze(controls()+[frame(),frame(mono=3100)],receipt())
        c=r['symbol_summary'][0]['counts']
        self.assertEqual(c['republished_changed_cts'],1)
        self.assertEqual(r['old_clock_republication_evidence'],0)

    def test_repeated_delivery_without_advanced_publication_not_evidence(self):
        rows=controls()+[frame(),frame(mono=3100,ts=100095,cts=100090)]
        r=analyze(rows,receipt())
        self.assertEqual(r['symbol_summary'][0]['counts']['same_update_same_content'],1)
        self.assertEqual(r['old_clock_republication_evidence'],0)

    def test_ping_does_not_refresh_book(self):
        rows=controls()+[frame(),frame(EXPIRIES[0]),dict(kind='message',source='spot',
              mono_ms=5000,wall_ms=105000,payload=dict(raw='{"op":"pong"}'))]
        r=analyze(rows,receipt())
        self.assertFalse(r['pairs'][4*8]['recent_l1_report'])
        self.assertEqual(r['controls']['pongs'],1)

    def test_update_id_jump_allowed(self):
        a=parse(frame())[1];b=parse(frame(mono=200,u=30,seq=40))[1]
        self.assertEqual(transition(a,b),[])

    def test_update_reset_regression_and_content_changes(self):
        a=parse(frame())[1]
        for b,reason in [(parse(frame(mono=200,u=1))[1],'SERVICE_RESET'),
                         (parse(frame(mono=200,u=9))[1],'UPDATE_REGRESSION'),
                         (parse(frame(mono=200,seq=9))[1],'SEQUENCE_REGRESSION'),
                         (parse(frame(mono=200,bid='99'))[1],'SAME_UPDATE_CHANGED_CONTENT')]:
            self.assertIn(reason,transition(a,b))

    def test_anomaly_latches_even_later_good_snapshot(self):
        r=analyze(controls()+[frame(),frame(EXPIRIES[0]),frame(mono=200,u=1),
                  frame(mono=900,u=11,seq=12)],receipt())
        self.assertFalse(r['pairs'][0]['legacy_condition'])
        self.assertIn('CONTINUITY_ANOMALY',r['pairs'][0]['reasons'])

    def test_capture_early_stop_never_forward_fills(self):
        r=analyze(controls()+[frame(),frame(EXPIRIES[0])],receipt(2000))
        self.assertNotIn('CAPTURE_ENDED',r['pairs'][8]['reasons'])
        self.assertIn('CAPTURE_ENDED',r['pairs'][16]['reasons'])
        self.assertFalse(r['pairs'][16]['recent_l1_report'])
        self.assertEqual(r['denominator'],960)

    def test_l1_size_pressure_gross_checked(self):
        s=parse(frame(ask='100000',bid='99999',qty='0.02504'))[1]
        f=parse(frame(EXPIRIES[0],qty='1'))[1]
        self.assertFalse(sizes(s,f))
        s['ask']=(D(100000),D('.025051'))
        self.assertTrue(sizes(s,f))
        f['bid']=(D(100),D('.024'))
        self.assertFalse(sizes(s,f))

    def test_wrong_symbol_socket_and_type(self):
        for mode in ('source','topic','type'):
            row=frame()
            if mode=='source': row['source']='linear'
            else:
                m=json.loads(row['payload']['raw']);m[mode]='wrong';row['payload']['raw']=json.dumps(m)
            with self.assertRaises(ValueError):parse(row)

    def test_time_order_and_reconnect_rejected(self):
        with self.assertRaisesRegex(ValueError,'RECEIVE_ORDER'):
            analyze([frame(mono=200),frame(mono=100)],receipt())
        r=receipt();r['reconnects']=1
        with self.assertRaisesRegex(ValueError,'CONTRACT'):analyze([],r)

    def test_all_symbol_and_pair_summaries(self):
        r=analyze(controls()+[frame(s) for s in SYMBOLS],receipt())
        self.assertEqual(len(r['symbol_summary']),9)
        self.assertEqual(len(r['term_summary']),8)
        self.assertEqual(r['legacy_qualified_pairs'],8)
        self.assertEqual(r['recent_l1_pairs'],32)
        self.assertFalse(r['old_l2_revalidated']);self.assertFalse(r['qualified'])

    def test_empty_side_not_qualified(self):
        row=frame();m=json.loads(row['payload']['raw']);m['data']['a']=[]
        row['payload']['raw']=json.dumps(m)
        r=analyze(controls()+[row,frame(EXPIRIES[0])],receipt())
        self.assertEqual(r['recent_l1_pairs'],0)

    def test_publication_future_clock_latches(self):
        r=analyze(controls()+[frame(ts=100200)],receipt())
        self.assertIn('CLOCK_ORDER',r['symbol_summary'][0]['continuity_issues'])

    def test_old_thresholds_boundary(self):
        rows=controls()+[frame(ts=100505,cts=100500),frame(EXPIRIES[0],ts=100005,cts=100000)]
        # Both receive after publication, and exactly 500ms engine skew at t=1s.
        for row in rows[2:]:row['mono_ms']=900;row['wall_ms']=100900
        r=analyze(rows,receipt())
        self.assertTrue(r['pairs'][0]['legacy_condition'])
        m=json.loads(rows[-1]['payload']['raw']);m['cts']=99999;rows[-1]['payload']['raw']=json.dumps(m)
        self.assertFalse(analyze(rows,receipt())['pairs'][0]['legacy_condition'])

    def test_protocol_failure_keeps_prefix_and_full_denominator(self):
        for bad in ('not json','[]','null','{"type":"delta"}', '{"op":"subscribe","success":false}'):
            rows=controls()+[frame(),frame(EXPIRIES[0]),dict(kind='message',source='linear',
                 mono_ms=2500,wall_ms=102500,payload=dict(raw=bad))]
            r=analyze(rows,receipt(2510))
            self.assertEqual(len(r['pairs']),960)
            self.assertTrue(r['pairs'][0]['legacy_condition'])
            self.assertIn('PROTOCOL_FAILURE',r['pairs'][16]['reasons'])
            self.assertFalse(r['pairs'][16]['recent_l1_report'])

    def test_protocol_fault_at_observation_boundary_is_not_usable(self):
        rows=controls()+[frame(),frame(EXPIRIES[0]),dict(kind='message',source='linear',
             mono_ms=1000,wall_ms=101000,payload=dict(raw='invalid'))]
        r=analyze(rows,receipt(1001))
        self.assertIn('PROTOCOL_FAILURE',r['pairs'][0]['reasons'])
        self.assertFalse(r['pairs'][0]['legacy_condition'])

    def test_recent_report_keeps_planned_four_second_definition(self):
        rows=controls()+[frame(mono=3500,ts=101500,cts=101490),
                          frame(EXPIRIES[0],mono=3500,ts=101500,cts=101490)]
        r=analyze(rows,receipt())
        self.assertTrue(r['pairs'][24]['recent_l1_report'])
        self.assertFalse(r['pairs'][24]['legacy_condition'])

    def test_collector_rejected_frame_not_normalized_to_valid(self):
        bads=[dict(op='subscribe',success=True,req_id='wrong'),dict(op='ping',success=False)]
        for price in ('1e2',' 100','9'*400):
            b=json.loads(frame()['payload']['raw']);b['data']['b'][0][0]=price;bads.append(b)
        b=json.loads(frame()['payload']['raw']);b['cts']=2**53;bads.append(b)
        for bad in bads:
            r=analyze([dict(kind='message',source='spot',mono_ms=100,wall_ms=100100,
                payload=dict(raw=json.dumps(bad)))],receipt(110))
            self.assertIsNotNone(r['protocol_failure'])
            self.assertEqual(r['legacy_qualified_pairs'],0)


if __name__=='__main__':unittest.main()
