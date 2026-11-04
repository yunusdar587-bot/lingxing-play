"""领星 OpenAPI 鉴权与请求客户端。

签名规则（领星开放平台文档）：
  1. 业务参数 + 公共参数(access_token, app_key, timestamp) 按 key ASCII 升序；
  2. 值为空字符串的不参与；dict/list 用紧凑 JSON（键排序）序列化；
  3. 拼成 k1=v1&k2=v2 后 MD5 取 32 位大写；
  4. 以 appId 为密钥 AES/ECB/PKCS5Padding 加密，Base64 输出即 sign，放在 query 中。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

TOKEN_PATH = "/api/auth-server/oauth/access-token"
REFRESH_PATH = "/api/auth-server/oauth/refresh"

TOKEN_ERROR_CODES = {2001001, 2001002, 2001003, 2001005}
RATE_LIMIT_CODES = {3001008, 103, 429}


class LingxingError(RuntimeError):
    def __init__(self, path: str, code: Any, message: str, payload: Any = None):
        super().__init__(f"{path} 返回 code={code}: {message}")
        self.path = path
        self.code = code
        self.payload = payload


class Transport(Protocol):
    def __call__(self, method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]: ...


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _canonical_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def canonical_string(params: dict) -> str:
    parts = []
    for k in sorted(params):
        v = params[k]
        if v is None or v == "":
            continue
        parts.append(f"{k}={_canonical_value(v)}")
    return "&".join(parts)


def aes_ecb_base64(key: str, plaintext: str) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key.encode("utf-8")), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def make_sign(app_id: str, params: dict) -> str:
    md5 = hashlib.md5(canonical_string(params).encode("utf-8")).hexdigest().upper()
    return aes_ecb_base64(app_id, md5)


class LingxingClient:
    """同一路径的请求串行执行（领星多数接口令牌桶容量为 1），遇限流等待后重试。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        base_url: str = "https://openapi.lingxing.com",
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 8,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.sleep = sleep
        self.max_retries = max_retries
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expire_at = 0.0
        self._token_lock = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- token ----
    def _auth_call(self, path: str, query: dict) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        status, raw = self.transport("POST", url, b"", {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise LingxingError(path, status, raw[:200].decode("utf-8", "replace"))
        if str(payload.get("code")) != "200" or not isinstance(payload.get("data"), dict):
            raise LingxingError(path, payload.get("code"), payload.get("msg") or payload.get("message") or "", payload)
        return payload["data"]

    def _store_token(self, data: dict) -> None:
        self._token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        self._token_expire_at = time.time() + float(data.get("expires_in") or 7200) - 120

    def access_token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._token_expire_at:
                return self._token
            if self._refresh_token and not force:
                try:
                    self._store_token(
                        self._auth_call(REFRESH_PATH, {"appId": self.app_id, "refreshToken": self._refresh_token})
                    )
                    return self._token  # type: ignore[return-value]
                except LingxingError as exc:
                    log.warning("刷新 token 失败，改为重新获取: %s", exc)
            self._store_token(self._auth_call(TOKEN_PATH, {"appId": self.app_id, "appSecret": self.app_secret}))
            return self._token  # type: ignore[return-value]

    # ---- request ----
    def _lock_for(self, path: str) -> threading.Lock:
        with self._locks_guard:
            return self._path_locks.setdefault(path, threading.Lock())

    def request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        method = method.upper()
        with self._lock_for(path):
            token_retried = False
            for attempt in range(self.max_retries + 1):
                token = self.access_token()
                public = {"access_token": token, "app_key": self.app_id, "timestamp": str(int(time.time()))}
                sign = make_sign(self.app_id, {**params, **public})
                query = {**public, "sign": sign}
                body: bytes | None = None
                headers = {"Content-Type": "application/json"}
                if method == "GET":
                    query.update({k: _canonical_value(v) for k, v in params.items()})
                else:
                    body = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
                status, raw = self.transport(method, url, body, headers)
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    payload = {"code": status, "message": raw[:200].decode("utf-8", "replace")}
                code = payload.get("code")
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = -1
                if code_int == 0 and status < 400:
                    return payload
                if code_int in TOKEN_ERROR_CODES and not token_retried:
                    token_retried = True
                    self.access_token(force=True)
                    continue
                if code_int in RATE_LIMIT_CODES or status == 429:
                    wait = min(60.0, 2.0 ** attempt)
                    log.info("%s 限流，%.0f 秒后重试（第 %d 次）", path, wait, attempt + 1)
                    self.sleep(wait)
                    continue
                raise LingxingError(path, code, str(payload.get("message") or payload.get("msg") or ""), payload)
            raise LingxingError(path, "rate_limited", f"重试 {self.max_retries} 次仍被限流")

    def paged(self, path: str, params: dict, length: int, method: str = "POST") -> list[dict]:
        """offset/length 翻页，直到取满 total 或返回空页。"""
        rows: list[dict] = []
        offset = 0
        while True:
            payload = self.request(method, path, {**params, "offset": offset, "length": length})
            page, total = extract_rows(payload)
            rows.extend(page)
            offset += len(page)
            if not page:
                return rows
            if total is not None:
                if offset >= total:
                    return rows
            elif len(page) < length:
                return rows


def extract_rows(payload: dict) -> tuple[list[dict], int | None]:
    data = payload.get("data")
    total = payload.get("total")
    if isinstance(data, dict):
        total = data.get("total", total)
        for key in ("list", "records", "rows", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = []
    if not isinstance(data, list):
        data = []
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return [r for r in data if isinstance(r, dict)], total
