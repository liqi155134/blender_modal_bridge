"""client.py — 云端 HTTP 客户端(纯 stdlib;所有方法阻塞,只允许在后台线程调用)。"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FarmError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable   # 网络类瞬时错误(broken pipe/超时/5xx)可重试


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

    # 单请求体上限:Modal 入口层实测 150MB 过、700MB 被 303 拒(具体阈值未公开)。
    # 超过就切块串行发,服务端经 Volume 中转拼接 —— 96MB 留足余量。
    UPLOAD_CHUNK = 96 << 20

    def upload(self, filepath: str, name: str, progress_cb=None, cancel_check=None) -> dict:
        """上传 .blend,返回 {blend_path, size_bytes}。≤96MB 单发;更大自动分块串行。
        progress_cb(sent_bytes, total_bytes) 按全局累计字节回调(网络线程)。
        cancel_check() 返回 True 时在下一个块边界中止(抛 FarmError;单发模式不可中止)。"""
        p = Path(filepath)
        size = p.stat().st_size
        if size <= self.UPLOAD_CHUNK:
            if cancel_check and cancel_check():
                raise FarmError("上传已被用户取消")
            return self._upload_once(p, name, size, progress_cb)
        import io
        import uuid
        upload_id = uuid.uuid4().hex
        total = (size + self.UPLOAD_CHUNK - 1) // self.UPLOAD_CHUNK
        d = None
        with open(p, "rb") as f:
            for index in range(total):
                if cancel_check and cancel_check():
                    raise FarmError("上传已被用户取消")
                chunk = f.read(self.UPLOAD_CHUNK)
                base = index * self.UPLOAD_CHUNK
                qs = urllib.parse.urlencode({
                    "key": self.key, "name": name,
                    "upload_id": upload_id, "index": index, "total": total})
                # 块级重试(分块架构的核心收益):网络瞬断只重传这一块,不整单报废。
                # 服务端同 upload_id+index 重写同文件,幂等安全。
                for attempt in range(3):
                    body = io.BytesIO(chunk)
                    if progress_cb:
                        body = _ProgressReader(
                            body, len(chunk),
                            lambda sent, _t, _b=base: progress_cb(_b + sent, size))
                    try:
                        d = self._upload_request(qs, body, len(chunk),
                                                 what=f"块 {index + 1}/{total}")
                        break
                    except FarmError as e:
                        if not e.retryable or attempt == 2:
                            raise
                        if cancel_check and cancel_check():
                            raise FarmError("上传已被用户取消") from None
                        time.sleep(2 * (attempt + 1))
        if not d or "blend_path" not in d:
            raise FarmError(f"分块上传收尾异常: {(d or {}).get('error') or d}")
        return d

    def _upload_once(self, p: Path, name: str, size: int, progress_cb) -> dict:
        qs = urllib.parse.urlencode({"key": self.key, "name": name})
        src = open(p, "rb")
        if progress_cb:
            src = _ProgressReader(src, size, progress_cb)
        d = self._upload_request(qs, src, size, what="上传")
        if "blend_path" not in d:
            raise FarmError(f"upload 响应异常: {d.get('error') or d}")
        return d

    def _upload_request(self, qs: str, body, length: int, what: str) -> dict:
        req = urllib.request.Request(
            f"{self._url('upload')}?{qs}", data=body, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(length)})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # ⚠ HTTP 错误状态永远不落成功路径:5xx 可重试、4xx 语义错不重试。
            # body 解析只为取错误详情(503 的 {"detail":...} 曾被当成功响应)。
            detail = ""
            try:
                body = json.loads(e.read().decode())
                detail = str(body.get("error") or body.get("detail") or body)[:300]
            except Exception:
                pass
            msg = f"{what} HTTP {e.code}" + (f": {detail}" if detail else "")
            raise FarmError(msg, retryable=e.code >= 500) from None
        except Exception as e:
            # broken pipe / 超时 / 连接 reset 等网络瞬断:可重试
            raise FarmError(f"{what} 失败: {e}", retryable=True) from e
        if "error" in d:
            raise FarmError(f"{what}: {d['error']}")   # 服务端语义错误:不重试
        return d

    def run(self, task: dict, blend_path: str | None) -> dict:
        """提交任务。task = {"task_type": "render", "render": {…}} 或
        {"task_type": "bake", "bake": {…}}(协议见 API.md)。"""
        body = dict(task)
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
        """原子下载:先写 .part 临时文件、校验大小、os.replace 落位,
        成功后才发 delete_only 清远端 —— 中断不留残缺产物、不丢远端数据。"""
        qs = urllib.parse.urlencode({"job_id": job_id, "path": volume_path,
                                     "key": self.key, "delete": 0})
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with urllib.request.urlopen(f"{self._url('fetch')}?{qs}", timeout=600) as r, \
                    open(tmp, "wb") as f:
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
            if total and size != total:
                raise FarmError(f"fetch 大小不符: {size}/{total}({volume_path})",
                                retryable=True)
            os.replace(tmp, dest)
        except urllib.error.HTTPError as e:
            raise FarmError(f"fetch HTTP {e.code}({volume_path})") from None
        finally:
            tmp.unlink(missing_ok=True)
        if delete_remote:
            try:   # 清理失败无害(Volume 里多躺一份),不影响下载结果
                qs2 = urllib.parse.urlencode({"job_id": job_id, "path": volume_path,
                                              "key": self.key, "delete_only": 1})
                urllib.request.urlopen(f"{self._url('fetch')}?{qs2}", timeout=30).read()
            except Exception:
                pass
        return size
