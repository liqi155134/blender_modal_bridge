#!/usr/bin/env python3
"""
farm_deploy.py — 容器内一键部署:建/更新 Secret(farm_key)→ modal deploy → 存 endpoint。

前置:modal SDK 已装且已鉴权(env MODAL_TOKEN_ID/MODAL_TOKEN_SECRET 或 ~/.modal.toml)。
FARM_* 部署参数经本脚本注入 env(裸跑 modal deploy 会全部丢失、回退默认值)。
用法:python3 farm_deploy.py [--gpu L40S] [--max-parallel 10] [--frame-timeout 1800] [--job-timeout 14400]
"""
import argparse
import json
import os
import re
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "farm_config.json"
APP_NAME = "blender-bridge"
VERSION = "0.2.0"


def load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpu", default="L40S")
    ap.add_argument("--max-parallel", type=int, default=10)
    ap.add_argument("--frame-timeout", type=int, default=1800)
    ap.add_argument("--job-timeout", type=int, default=14400)
    args = ap.parse_args()
    if not 1 <= args.max_parallel <= 100:
        ap.error("--max-parallel 必须在 1..100(费用护栏)")
    if args.frame_timeout < 60:
        ap.error("--frame-timeout 必须 ≥ 60 秒")
    if args.job_timeout <= args.frame_timeout:
        ap.error("--job-timeout 必须大于 --frame-timeout")

    cfg = load_cfg()
    farm_key = cfg.get("farm_key") or ("fk-" + pysecrets.token_urlsafe(24))  # 复用旧 key,重部署不换锁

    env = {**os.environ,
           "FARM_GPU": args.gpu,
           "FARM_MAX_PARALLEL": str(args.max_parallel),
           "FARM_FRAME_TIMEOUT": str(args.frame_timeout),
           "FARM_JOB_TIMEOUT": str(args.job_timeout),
           "FARM_VERSION": VERSION}

    print(f"[1/2] 建/更新 Secret({APP_NAME}-secrets)…")
    r = subprocess.run([sys.executable, "-m", "modal", "secret", "create", "--force",
                        f"{APP_NAME}-secrets", f"FARM_API_KEY={farm_key}"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"secret 创建失败:{r.stderr[-500:]}\n(modal token 配好了吗?)")

    print(f"[2/2] modal deploy(gpu={args.gpu},首次要构建镜像,分钟级)…")
    # cwd=modal_app/:add_local_python_source 按部署时模块名解析,必须在该目录跑
    proc = subprocess.Popen([sys.executable, "-m", "modal", "deploy", "farm_app.py"],
                            cwd=str(HERE / "modal_app"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        print("  " + line.rstrip(), flush=True)
        tail.append(line)
    if proc.wait() != 0:
        sys.exit("deploy 失败(日志见上)")

    # 从输出解析 endpoint base:https://<ws>--blender-bridge-run.modal.run → https://<ws>--blender-bridge
    m = re.search(rf"https://[\w\-]+--{re.escape(APP_NAME)}-\w+\.modal\.run", "".join(tail))
    if not m:
        sys.exit(f"✓ 部署完成,但没解析到 endpoint —— 手动填 farm_config.json(key: {farm_key})")
    base = re.sub(r"-(upload|run|status|cancel|fetch|health)\.modal\.run$", "", m.group(0))
    CFG.write_text(json.dumps({"endpoint": base, "farm_key": farm_key}, indent=2))
    try:
        CFG.chmod(0o600)
    except Exception:
        pass
    print(f"\n✓ 部署完成。endpoint + farm_key 已写 {CFG}")
    print(f"  endpoint: {base}")
    print(f"  farm_key: {farm_key}")
    print("  → Blender addon preferences 里填这两项")


if __name__ == "__main__":
    main()
