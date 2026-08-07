#!/usr/bin/env python3
"""
smoke_test.py — 部署后全链路冒烟(纯 stdlib):demo 场景 8 帧 → 轮询进度 → 下载 mp4。
用法:python3 smoke_test.py [--frames 1-8] [--output video] [--cancel-after N(秒,测取消)]
读 farm_config.json 的 endpoint/farm_key。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

cfg = json.loads((HERE / "farm_config.json").read_text())
BASE, KEY = cfg["endpoint"], cfg["farm_key"]


def post(label, body):
    req = urllib.request.Request(f"{BASE}-{label}.modal.run",
                                 data=json.dumps({**body, "auth_key": KEY}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(label, **params):
    qs = urllib.parse.urlencode({**params, "key": KEY})
    with urllib.request.urlopen(f"{BASE}-{label}.modal.run?{qs}", timeout=30) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="render", choices=["render", "bake"])
    ap.add_argument("--frames", default="1-8")
    ap.add_argument("--output", default="video", choices=["video", "frames"])
    ap.add_argument("--format", default="PNG", choices=["PNG", "OPEN_EXR"])
    ap.add_argument("--cancel-after", type=int, default=0)
    args = ap.parse_args()

    print("health:", get("health"))
    if args.task == "bake":
        payload = {"task_type": "bake",
                   "bake": {"objects": ["Cube"], "passes": ["NORMAL", "AO"],
                            "resolution": 512, "file_format": args.format}}
    else:
        payload = {"render": {"frames": args.frames,   # 复合 spec 直接透传("1,3-5,8:2")
                              "output": args.output, "file_format": args.format}}
    d = post("run", payload)
    if "id" not in d:
        sys.exit(f"提交失败: {d}")
    job_id = d["id"]
    print(f"job {job_id}  gpu={d.get('gpu')}")
    t0 = time.time()
    while True:
        time.sleep(3)
        s = get("status", job_id=job_id)
        p = s.get("progress") or {}
        print(f"  [{s.get('status')}] {p.get('step', 0)}/{p.get('total', '?')} 帧"
              f" · {p.get('s_it', '?')}s/帧 · 已 {int(time.time() - t0)}s", flush=True)
        if args.cancel_after and time.time() - t0 > args.cancel_after:
            print("cancel:", post("cancel", {"job_id": job_id}))
            return
        if s.get("status") in ("completed", "failed", "cancelled"):
            break
    if s.get("status") != "completed":
        sys.exit(f"未成功: {s.get('status')} — {s.get('error', '')[:400]}")
    if s.get("warnings"):
        print(f"⚠ warnings: {s['warnings']}")
    print(f"render_device: {s.get('render_device')}(期望 OPTIX)")
    out_dir = Path("/tmp/farm_smoke") / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for o in s.get("outputs") or []:
        qs = urllib.parse.urlencode({"job_id": job_id, "path": o["volume_path"],
                                     "key": KEY, "delete": 1})
        dest = out_dir / o["filename"]
        with urllib.request.urlopen(f"{BASE}-fetch.modal.run?{qs}", timeout=600) as r, \
                open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        print(f"✓ {dest}  ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
