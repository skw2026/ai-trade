#!/usr/bin/env python3
"""All market/return fixtures are invented; never reads local historical data."""
import copy
import argparse
import csv
import io
import json
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlencode
import mvp_reference_inputs as inputs
import mvp_reference_verdict as verdict
import mvp_reference_pipeline as pipeline

BINARY = None  # Required explicit build identity; never fall back to ROOT/build.


class ReferenceInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.contract = inputs.load_contract()
        self.contract.update(source_start_utc="2025-06-29T23:55:00Z",
                             evaluation_start_utc="2025-06-30T00:00:00Z",
                             end_exclusive_utc="2025-07-02T00:05:00Z")
        self.start, self.evaluation, self.end = inputs.window(self.contract)
        self.manifest = {"schema": "mvp_public_archive_v1", "contract_sha256": inputs.sha(json.dumps(self.contract,sort_keys=True).encode()), "pages": []}
        for kind in ("trade", "mark", "funding"):
            funding = kind == "funding"
            grid = 28800000 if funding else 300000
            times = list(range(((self.start+grid-1)//grid)*grid, self.end, grid))[::-1]
            rows = ([{"symbol": "BTCUSDT", "fundingRateTimestamp": str(t), "fundingRate": "0.0001"} for t in times]
                    if funding else [[str(t), "100", "102", "99", "101"] + (["1", "100"] if kind == "trade" else []) for t in times])
            result = {"category": "linear", "symbol": "BTCUSDT", "list": rows}
            self.write_response(kind, {"retCode": 0, "result": result})
            q = {"category": "linear", "symbol": "BTCUSDT", "limit": 200 if funding else 1000,
                 "startTime" if funding else "start": self.start, "endTime" if funding else "end": self.end-1}
            if not funding:
                q["interval"] = "5"
            self.manifest["pages"].append({"kind": kind, "file": kind+".json",
                "url": "https://api.bybit.com"+self.contract["input"][kind+"_endpoint"]+"?"+urlencode(q),
                "sha256": inputs.sha((self.root/(kind+".json")).read_bytes()), "received_at_ms": self.end+1})

    def tearDown(self):
        self.tmp.cleanup()

    def write_response(self, kind, value):
        (self.root/(kind+".json")).write_text(json.dumps(value))

    def mutate(self, kind, fn):
        path = self.root/(kind+".json")
        obj = json.loads(path.read_text()); fn(obj); self.write_response(kind, obj)
        next(p for p in self.manifest["pages"] if p["kind"] == kind)["sha256"] = inputs.sha(path.read_bytes())

    def compile(self):
        path = self.root/"manifest.json"; path.write_text(json.dumps(self.manifest))
        return inputs.compile_archive(path, self.contract)

    def test_complete_input_and_funding_not_forward_filled(self):
        raw, proof = self.compile()
        rows = list(csv.DictReader(io.StringIO(raw.decode())))
        self.assertTrue(proof["complete"])
        self.assertTrue(proof["synthetic_only"])
        self.assertNotEqual(proof["contract_sha256"],inputs.CONTRACT_SHA256)
        self.assertEqual(len(rows), (self.end-self.start)//300000)
        self.assertEqual(rows[0]["execution_enabled"], "0")
        self.assertEqual(rows[1]["execution_enabled"], "1")
        self.assertEqual(rows[1]["funding_rate_per_interval"], "0.0001")
        self.assertEqual(rows[2]["funding_rate_per_interval"], "0")
        self.assertEqual(rows[-1]["funding_rate_per_interval"], "0.0001")
        self.assertEqual(rows[0]["mark_high"], "102")

    def test_hash_tamper(self):
        (self.root/"mark.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "RAW_HASH"): self.compile()

    def test_missing_trade_bar(self):
        self.mutate("trade", lambda x: x["result"]["list"].pop(5))
        with self.assertRaisesRegex(ValueError, "CANDLE_COVERAGE"): self.compile()

    def test_missing_funding_event(self):
        self.mutate("funding", lambda x: x["result"]["list"].pop())
        with self.assertRaisesRegex(ValueError, "FUNDING_GRID"): self.compile()

    def test_funding_schedule_change_not_zero_filled(self):
        self.mutate("funding", lambda x: x["result"]["list"][1].update(fundingRateTimestamp=str(self.end-300000-14400000)))
        with self.assertRaisesRegex(ValueError, "FUNDING_GRID"): self.compile()

    def test_wrong_symbol(self):
        self.mutate("mark", lambda x: x["result"].update(symbol="ETHUSDT"))
        with self.assertRaisesRegex(ValueError, "RESPONSE_SYMBOL"): self.compile()

    def test_wrong_domain(self):
        self.manifest["pages"][0]["url"] = self.manifest["pages"][0]["url"].replace("api.bybit.com", "example.com")
        with self.assertRaisesRegex(ValueError, "PUBLIC_URL"): self.compile()

    def test_nonfinite_and_ohlc(self):
        self.mutate("mark", lambda x: x["result"]["list"][0].__setitem__(2,"NaN"))
        with self.assertRaisesRegex(ValueError, "NONFINITE"): self.compile()

    def test_unfinished(self):
        self.manifest["pages"][0]["received_at_ms"] = self.end-1
        with self.assertRaisesRegex(ValueError, "UNFINISHED"): self.compile()

    def test_duplicate_row(self):
        self.mutate("trade",lambda x:x["result"]["list"].insert(1,x["result"]["list"][0]))
        with self.assertRaisesRegex(ValueError, "ORDER_OR_RANGE"): self.compile()

    def test_unsafe_path_and_contract(self):
        self.manifest["pages"][0]["file"] = "../escape.json"
        with self.assertRaisesRegex(ValueError, "PATH_ESCAPE"): self.compile()

    def test_terminal_funding_boundary(self):
        self.contract["end_exclusive_utc"]="2025-07-02T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "TERMINAL_FUNDING"): self.compile()

    def test_strict_json_duplicates(self):
        with self.assertRaisesRegex(ValueError,"DUPLICATE_JSON"):
            inputs.strict_json('{"x":1,"x":2}')

    def test_contract_values_and_config_scope(self):
        c=inputs.load_contract()
        self.assertEqual(c["capital"]["leverage"],2)
        self.assertEqual(c["capital"]["maintenance_rate"],0.01)
        old=(inputs.ROOT/"config/bybit.replay.mvp.yaml").read_text()
        new=(inputs.ROOT/"config/bybit.replay.mvp-reference.yaml").read_text()
        expected=old.replace("# Engineering-only fixed original-MVP reference, NOT a qualified candidate.",
                            "# Explicit OFFLINE reference capital model v1, NOT Bybit account/historical rules.")
        expected=expected.replace("  closed_bar_mvp: true","  closed_bar_mvp: true\n  replay_reference_account: true")
        expected=expected.replace("original-mvp-causal-reference","original-mvp-capital-reference-v1")
        expected=expected.replace("./.artifacts/original-mvp-replay","./.artifacts/mvp-capital-reference-v1")
        self.assertEqual(new,expected)

    def composed_run(self, slope):
        def trend(obj):
            for row in obj["result"]["list"]:
                p=500+slope*((int(row[0])-self.start)//300000)
                row[1:5]=[str(p),str(p+slope),str(p),str(p+slope)]
        self.mutate("trade",trend);self.mutate("mark",trend)
        self.compile()  # writes only synthetic manifest/fixture
        plan_path=self.root/"prepared"/"run-plan.json"
        plan=pipeline.prepare(self.root/"manifest.json",self.root/"prepared",BINARY,self.contract)
        self.assertEqual(plan["binary"],str(BINARY.resolve()))
        self.assertEqual(plan["binary_sha256"],pipeline.digest(BINARY))
        self.assertTrue(plan["synthetic_only"])
        with self.assertRaisesRegex(ValueError,"RUN_PLAN_SCOPE"):
            pipeline.verify(plan_path,synthetic=False)
        with self.assertRaisesRegex(ValueError,"APPROVAL_REQUIRED"):
            pipeline.execute(plan_path)
        result=pipeline.execute(plan_path,synthetic=True)
        self.assertEqual(len(result["receipts"]),2,result)
        self.assertEqual([r["exit_code"] for r in result["receipts"]],[0,0],result)
        self.assertTrue(all(r["argv"][0] == str(BINARY.resolve()) for r in result["receipts"]))
        self.assertTrue(result["synthetic_only"])
        self.assertFalse(result["economic_qualification"])
        self.assertNotEqual(plan["configs"]["base"]["sha256"],plan["configs"]["stress"]["sha256"])
        runs={}
        for scenario in ("base","stress"):
            events=verdict.parse_events((self.root/"prepared"/(scenario+".log")).read_text())
            runs[scenario]=events
            self.assertTrue(events["terminals"][0]["flat"])
            self.assertEqual(events["terminals"][0]["collateral"],0)
        with self.assertRaises(FileExistsError):pipeline.execute(plan_path,synthetic=True)
        with (self.root/"prepared"/"base.yaml").open("a") as f:f.write("# changed\n")
        with self.assertRaisesRegex(ValueError,"CONFIG_CHANGED"):
            pipeline.verify(plan_path,synthetic=True)
        return result,runs

    def test_stress_cost_correctly_rejects_weak_synthetic_signal(self):
        # Preserve the original failed fixture: about 20 bps < stress hurdle 27.
        result,runs=self.composed_run(slope=1)
        self.assertTrue(runs["base"]["fills"])
        self.assertEqual(runs["stress"]["fills"],[])
        self.assertEqual(result["decision"],"REJECT")
        self.assertIn("stress:LOW_ACTIVITY",result["reason"])
        self.assertIn("ORDER_FILTERED_COST",(self.root/"prepared/stress.log").read_text())

    def test_raw_to_two_cost_scenarios_real_binary_and_bound_receipts(self):
        # Separate invented execution fixture: initially 2/500 = 40 bps > 27.
        # No strategy/cost change, historical sample, or positive-profit target.
        _,runs=self.composed_run(slope=2)
        for scenario,events in runs.items():
            self.assertTrue(events["fills"],scenario+": no synthetic execution")
            rate=0.00055 if scenario=="base" else 0.0011
            for fill in events["fills"]:
                self.assertAlmostEqual(fill["fee"],fill["qty"]*fill["price"]*rate,places=7)

    def test_malformed_log_stops_before_second_scenario(self):
        self.compile()
        pipeline.prepare(self.root/"manifest.json",self.root/"prepared",BINARY,self.contract)
        def malformed(*args, **kwargs):
            self.assertLessEqual(set(kwargs["env"]),{"PATH","LANG","LC_ALL","TZ"})
            kwargs["stdout"].write('REFERENCE_BAR_JSON {"ts":NaN}\n')
            return SimpleNamespace(returncode=0)
        with mock.patch.object(pipeline.subprocess,"run",side_effect=malformed) as run:
            result=pipeline.execute(self.root/"prepared/run-plan.json",synthetic=True)
        self.assertEqual(run.call_count,1)
        self.assertEqual(result["decision"],"INSUFFICIENT")
        self.assertEqual(len(result["receipts"]),1)

    def test_post_run_identity_tamper_invalidates_explicit_risk_reject(self):
        self.compile()
        pipeline.prepare(self.root/"manifest.json",self.root/"prepared",BINARY,self.contract)
        def tamper(*args, **kwargs):
            kwargs["stdout"].write('REFERENCE_STOP_JSON {"reason":"REJECT_REFERENCE_MAINTENANCE"}\n')
            with (self.root/"prepared/replay.csv").open("a") as f:f.write("changed\n")
            return SimpleNamespace(returncode=1)
        with mock.patch.object(pipeline.subprocess,"run",side_effect=tamper) as run:
            result=pipeline.execute(self.root/"prepared/run-plan.json",synthetic=True)
        self.assertEqual(run.call_count,1)
        self.assertEqual(result["decision"],"INSUFFICIENT")


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.contract=inputs.load_contract()
        self.contract.update(source_start_utc="2025-06-29T23:55:00Z", evaluation_start_utc="2025-06-30T00:00:00Z",
                             end_exclusive_utc="2025-07-02T00:05:00Z")
        source,start,end=inputs.window(self.contract)
        bars=[{"ts":t,"warmup":t<=start,"signal":t>start,"equity":10000+(t-start)/300000,
               "funding_uncertainty":0,"drawdown_upper":0.001} for t in range(source+300000,end+1,300000)]
        fills=[]
        for day in range(2):
            for order in range(4):
                for part in range(2):
                    fills.append({"ts":start+day*verdict.DAY+(order*6*60+5)*60000,
                        "order":f"order-{day}-{order}","fill":f"fill-{day}-{order}-{part}","qty":0.001,"price":100,"fee":0.000055})
        run={"bars":bars,"fills":fills,"terminals":[{"flat":True,"pending":False,"equity":bars[-1]["equity"]-1,
             "collateral":0,"funding_uncertainty":0,"drawdown_upper":0.001}],"stops":[]}
        self.runs={"base":copy.deepcopy(run),"stress":copy.deepcopy(run)}

    def result(self): return verdict.assess(self.runs,self.contract)

    def test_positive_is_only_further_review(self):
        r=self.result()
        self.assertEqual(r["decision"],"WORTH_FURTHER_REVIEW_NOT_PROFIT_QUALIFIED")
        self.assertFalse(r["demo_activation_authorized"])

    def test_partial_fills_do_not_create_activity(self):
        self.runs["base"]["fills"]=[f for f in self.runs["base"]["fills"] if f["order"].endswith(("0","1"))]
        self.assertEqual(self.result()["decision"],"REJECT")
        self.assertIn("LOW_ACTIVITY",self.result()["reason"])

    def test_missing_tail_insufficient(self):
        self.runs["base"]["bars"].pop()
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_risk_reject_priority(self):
        self.runs["base"]["stops"]=[{"reason":"REJECT_REFERENCE_MAINTENANCE"}]
        self.runs["stress"]["bars"]=[]
        self.assertEqual(self.result()["decision"],"REJECT")

    def test_intrabar_stop(self):
        self.runs["base"]["stops"]=[{"reason":"INSUFFICIENT_INTRABAR_CONTROL_PATH"}]
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_nonpositive_cost_net_reject(self):
        self.runs["stress"]["terminals"][0]["equity"]=9999
        self.assertEqual(self.result()["decision"],"REJECT")

    def test_uncertainty_can_change_sign(self):
        run=self.runs["stress"]
        run["bars"][-1]["funding_uncertainty"]=1000
        run["terminals"][0]["funding_uncertainty"]=1000
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_no_false_terminal_flat(self):
        self.runs["base"]["terminals"][0]["collateral"]=0.001
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_terminal_drawdown_cannot_erase_previous_bound(self):
        self.runs["base"]["terminals"][0]["drawdown_upper"]=0
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_drawdown_bound_must_be_monotonic(self):
        self.runs["base"]["bars"][-1]["drawdown_upper"]=0
        self.assertEqual(self.result()["decision"],"INSUFFICIENT")

    def test_log_parsing(self):
        log='[INFO] REFERENCE_STOP_JSON {"reason":"INSUFFICIENT_RUNTIME"}'
        self.assertEqual(verdict.parse_events(log)["stops"][0]["reason"],"INSUFFICIENT_RUNTIME")

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",type=Path,required=True)
    args,remaining=parser.parse_known_args()
    BINARY=args.binary.resolve()
    if not BINARY.is_file():parser.error("--binary must identify the current built trade_bot")
    unittest.main(argv=[__file__,*remaining])
