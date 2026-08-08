"""ruff 全仓静态检查。

compileall 抓不到 F821(未定义名):漏 import 照样编译通过,要到运行时才 NameError
(2026-08-08 实锤:分块上传 done marker 用了 json 却没 import,尾块合并必炸)。
farm_app / addon 依赖 modal 与 bpy 运行时,没法在 CI 里跑真集成 —— 静态检查是
这些路径目前唯一的自动防线,ruff 必须保持 0 错误。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff 未安装")
def test_ruff_clean():
    r = subprocess.run(["ruff", "check", str(ROOT)], capture_output=True, text=True)
    assert r.returncode == 0, f"ruff check 未通过:\n{r.stdout}{r.stderr}"
