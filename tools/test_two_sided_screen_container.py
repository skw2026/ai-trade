#!/usr/bin/env python3
"""Native-container synthetic lifecycle; no market access or original data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import two_sided_screen_remote as remote

ROOT = Path(__file__).resolve().parents[1]


def run(args, payload=None):
    args = list(args)
    token = uuid.uuid4().hex
    name = "two-sided-synthetic-" + token
    if "--name" in args:
        args[args.index("--name") + 1] = name
    else:
        args[2:2] = ["--name", name]
    args[2:2] = ["--label", "ai-trade.synthetic-check=" + token]
    try:
        result = subprocess.run(args, input=payload, capture_output=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        checked = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "ai-trade.synthetic-check"}}', name],
                                 capture_output=True, timeout=10, check=False)
        if checked.returncode == 0:
            if checked.stdout.strip().decode() != token:
                raise RuntimeError("SYNTHETIC_TIMEOUT_IDENTITY_CHANGED") from None
            stopped = subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True, timeout=15, check=False)
            if stopped.returncode:
                raise RuntimeError("SYNTHETIC_TIMEOUT_STOP_FAILED") from None
        raise RuntimeError("SYNTHETIC_CONTAINER_TIMEOUT") from None
    if result.returncode:
        # Synthetic-only output; not a credential-bearing transport.
        raise RuntimeError("SYNTHETIC_CONTAINER_FAILED:" + result.stderr.decode()[-2000:])
    return result


def main():
    contract = json.loads((ROOT / (remote.PREFIX + ".contract.json")).read_text())
    token = uuid.uuid4().hex
    temporary = Path(tempfile.mkdtemp(prefix="two-sided-synthetic-")).resolve()
    template, stage = temporary / "template", temporary / "stage"
    capture_created = False
    try:
        template.mkdir(mode=0o755)
        template.chmod(0o755)
        stage.mkdir(mode=0o777)
        stage.chmod(0o777)
        for directory in (template / "tools", template / "docs", template / "docs/plans", stage / "code"):
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        for name in ("forward_evidence.py", "collect_bybit_microstructure.py", "two_sided_quote_screen.py",
                     "two_sided_screen_remote.py", "test_two_sided_quote_screen.py", "audit_two_sided_quote_screen.py"):
            for destination in (template / "tools", stage / "code"):
                target = destination / name
                target.write_bytes((ROOT / "tools" / name).read_bytes())
                target.chmod(0o444)
        for suffix in (".contract.json", ".manifest.json"):
            target = template / (remote.PREFIX + suffix)
            target.write_bytes((ROOT / (remote.PREFIX + suffix)).read_bytes())
            target.chmod(0o444)
        base = ["docker", "run", "--rm", "--pull=never", "--platform", contract["platform"],
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--user", "65534:65534", "--memory=512m", "--cpus=1", "--pids-limit=64",
                "--mount", "type=bind,source=%s,target=/synthetic,readonly" % template,
                "--mount", "type=bind,source=%s,target=/staging" % stage,
                "--entrypoint", "python3", contract["image"], "-B", "-c"]
        builder = ("import os,sys,json\nfrom pathlib import Path\n"
                   "sys.path.insert(0,'/synthetic/tools')\nimport test_two_sided_quote_screen as t\n"
                   "os.umask(0o077)\np=Path('/staging/capture'); p.mkdir(mode=0o700)\n"
                   "m=t.write_fixture(p)\nassert p.stat().st_uid==os.getuid()==65534\n"
                   "assert (p/'segment-000').stat().st_mode&0o777==0o700\nprint(json.dumps(m))\n")
        capture_created = True  # cleanup also covers a partially written fixture
        manifest = json.loads(run(base + [builder]).stdout)
        c = dict(contract, segments=1)
        spec = remote.docker_spec(c, stage, "12345")
        spec[spec.index("--name") + 1] = "two-sided-synthetic-" + token
        assertions = ("import os,socket,fcntl,struct\nassert os.getuid()==os.getgid()==65534\n"
                      "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
                      "for _,name in socket.if_nameindex():\n"
                      " flags=struct.unpack_from('H',fcntl.ioctl(s.fileno(),0x8913,struct.pack('256s',name.encode())),16)[0]\n"
                      " assert name=='lo' or not(flags&1)\n"
                      "s.close()\nassert len(open('/proc/net/route').read().splitlines())==1\n"
                      "try:\n open('/evidence/forbidden-write','x')\n"
                      "except OSError as e:\n assert e.errno in (13,30)\n"
                      "else:\n raise AssertionError('EVIDENCE_NOT_READONLY')\n")
        spec[-1] = assertions + spec[-1]
        payload = remote.canonical({"source": (ROOT / "tools/two_sided_quote_screen.py").read_text(),
                                    "contract": c, "manifest": manifest})
        result = json.loads(run(spec, payload).stdout)
        assert result["status"] == "SCREEN_COMPLETED"
        assert result["replayed_segments"] == 1 and result["summary"]["ALL"]["cycles"] == 3
        assert not result["g4_passed"] and not result["actual_fills_identified"]
        print(json.dumps({"status": "SYNTHETIC_CONTAINER_PASS", "uid": 65534, "read_only": True,
                          "network_none": True, "same_image": contract["image"], "full_synthetic_screen": True}))
    finally:
        primary_failure = __import__("sys").exc_info()[0] is not None
        cleanup_ok = True
        if capture_created:
            cleanup = ("import os,shutil\nfrom pathlib import Path\np=Path('/staging/capture')\n"
                       "if p.exists():\n assert not p.is_symlink() and p.stat().st_uid==os.getuid()==65534\n"
                       " assert set(v.name for v in p.iterdir())<={'segment-000','started.json'}\n shutil.rmtree(p)\n")
            try:
                run(base + [cleanup])
            except Exception:
                cleanup_ok = False
                if primary_failure:
                    print(json.dumps({"cleanup_failed": True, "synthetic_directory_retained": str(temporary)}))
                else:
                    raise
        # Only this invocation's freshly generated synthetic directory is removed.
        if cleanup_ok:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
