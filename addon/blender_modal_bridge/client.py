"""client.py — 云端 HTTP 客户端(纯 stdlib;所有方法阻塞,只允许在后台线程调用)。"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FarmError(RuntimeError):
    pass


class _ProgressReader:
    """包装上传文件对象:urllib 逐块 read 时统计已发送字节回调进度(cb 在网络线程被调)。"""

    def __init__(self, f, total: int, cb):
        self._f, self._total, self._cb, self._sent = f, total, cb, 0

    def read(self, n: int = -1):
        chunk = self._f.read(n)
        self._sent += len(chunk)
        try:
            self._cb(self._sent, self._total)
        except Exception:
            pass
        return chunk

    def close(self):
        self._f.close()


class FarmClient:
    def __init__(self, endpoint_base: str, key: str, timeout: int = 60):
        """endpoint_base 形如 https://<workspace>--blender-bridge(farm_deploy 打印的)。"""
        if not endpoint_base or "--" not in endpoint_base:
            raise FarmError("endpoint 形如 https://<workspace>--blender-bridge")
        self.base = endpoint_base.rstrip("/")
        self.key = key or ""
        self.timeout = timeout

    def _url(self, label: str) -> str:
        return f"{self.base}-{label}.modal.run"

    def _get(self, label: str, timeout: int | None = None, **params) -> dict:
        qs = urllib.parse.urlencode({**params, "key": self.key})
        return self._req(f"{self._url(label)}?{qs}", None, timeout)

    def _post(self, label: str, body: dict, timeout: int | None = None) -> dict:
        return self._req(self._url(label), {**body, "auth_key": self.key}, timeout)

    def _req(self, url: str, body: dict | None, timeout: int | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FarmError("401 — farm_key 不对/缺失") from None
            try:
                return json.loads(e.read().decode())
            except Exception:
                raise FarmError(f"HTTP {e.code}: {url}") from None
        except Exception as e:
            raise FarmError(f"请求失败: {e}") from e

    # ── 协议 ──
    def health(self) -> dict:
        return self._get("health", timeout=15)

    def upload(self, filepath: str, name: str, progress_cb=None) -> dict:
        """流式上传 .blend,返回 {blend_path, size_bytes}。大文件给长超时。
        progress_cb(sent_bytes, total_bytes) 每个网络块回调一次(网络线程)。"""
        p = Path(filepath)
        size = p.stat().st_size
        qs = urllib.parse.urlencode({"key": self.key, "name": name})
        src = open(p, "rb")
        if progress_cb:
            src = _ProgressReader(src, size, progress_cb)
        req = urllib.request.Request(
            f"{self._url('upload')}?{qs}", data=src, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(size)})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read().decode())
            except Exception:
                raise FarmError(f"upload HTTP {e.code}") from None
        except Exception as e:
            raise FarmError(f"upload 失败: {e}") from e
        if "blend_path" not in d:
            raise FarmError(f"upload 响应异常: {d.get('error') or d}")
        return d

    def run(self, render: dict, blend_path: str | None) -> dict:
        body = {"task_type": "render", "render": render}
        if blend_path:
            body["blend_path"] = blend_path
        d = self._post("run", body)
        if "id" not in d:
            raise FarmError(f"run 失败: {d.get('error') or d}")
        return d

    def status(self, job_id: str) -> dict:
        return self._get("status", job_id=job_id, timeout=20)

    def cancel(self, job_id: str) -> dict:
        """⚠ 返回带 error 表示取消失败、云端仍在计费 —— 调用方必须透出。"""
        return self._post("cancel", {"job_id": job_id}, timeout=30)

    def fetch(self, job_id: str, volume_path: str, dest_path: str,
              delete_remote: bool = True, progress_cb=None) -> int:
        qs = urllib.parse.urlencode({"job_id": job_id, "path": volume_path,
                                     "key": self.key, "delete": int(delete_remote)})
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(f"{self._url('fetch')}?{qs}", timeout=600) as r, \
                    open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                size = 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    size += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(size, total)
                        except Exception:
                            pass
                return size
        except urllib.error.HTTPError as e:
            raise FarmError(f"fetch HTTP {e.code}({volume_path})") from None
