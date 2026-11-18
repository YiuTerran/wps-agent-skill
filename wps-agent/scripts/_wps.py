"""WPS Agent authentication and HTTPS transport."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


TOKEN_URL = "https://account.wps.cn/api/authorization/agent/v1/token"
API_ORIGIN = "https://openapi.wps.cn"
CONTENT_ORIGIN = "https://api.wps.cn"
LINK_HOSTS = {"www.kdocs.cn", "kdocs.cn", "365.kdocs.cn", "weboffice.wps.cn"}
_SHELL_ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?AGENT_API_KEY=(.*)")
_SHELL_LITERAL = re.compile(r"""(['"]?)([A-Za-z0-9._:/+=@%-]+)\1(?:[ \t]+#.*)?[ \t]*""")


def use_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


class WpsError(RuntimeError):
    def __init__(self, operation, kind, *, status=None, code=None, headers=None, uncertain=False):
        self.status = status
        self.code = code
        self.kind = kind
        self.uncertain = uncertain
        details = [kind]
        if status is not None:
            details.append(f"HTTP {status}")
        if code is not None:
            details.append(f"code={code}")
        if headers:
            request_id = headers.get("X-Request-Id") or headers.get("X-Kso-Request-Id") or ""
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
                details.append(f"request_id={request_id}")
            retry_after = headers.get("Retry-After") or ""
            if status == 429 and retry_after.isascii() and retry_after.isdecimal():
                details.append(f"Retry-After={retry_after}s")
        super().__init__(f"{operation}：{'，'.join(details)}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def segment(value) -> str:
    value = str(value)
    if not value or value in {".", ".."} or any(ord(c) < 32 for c in value):
        raise ValueError("无效的 WPS 路径参数")
    return urllib.parse.quote(value, safe="")


def link_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9]+", value):
        return value
    parsed = urllib.parse.urlsplit(value)
    match = re.fullmatch(r"/l/([A-Za-z0-9]+)/?", parsed.path)
    if (parsed.scheme == "https" and parsed.netloc.lower() in LINK_HOSTS and match):
        return match[1]
    raise ValueError("需要 WPS HTTPS 分享链接或 link_id")


def _numeric_code(payload, field="code"):
    value = payload.get(field) if isinstance(payload, dict) else None
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    return None


def _http_kind(status, code):
    if status == 401 or code in {401, 401000001}:
        return "访问令牌无效或已过期"
    if status == 403 or (code is not None and str(code).startswith("403")):
        return "权限不足，请检查助理授权和文档权限"
    if status == 429 or (code is not None and str(code).startswith("429")):
        return "触发限流，请等待恢复"
    if 300 <= status < 400:
        return "已拦截重定向"
    return "WPS 请求失败"


def _encode(body):
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return config_home / "wps-agent" / "config.json"


def _shell_api_key() -> str | None:
    if os.name != "posix":
        return None

    home = Path.home()
    zsh_files = (".zshrc", ".zprofile", ".zshenv")
    bash_files = (".bashrc", ".bash_profile", ".bash_login")
    shell = Path(os.environ.get("SHELL", "")).name
    filenames = (*bash_files, *zsh_files) if shell == "bash" else (*zsh_files, *bash_files)
    for filename in (*filenames, ".profile"):
        path = home / filename
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in reversed(lines):
            assignment = _SHELL_ASSIGNMENT.fullmatch(line)
            if assignment:
                literal = _SHELL_LITERAL.fullmatch(assignment[1])
                if literal:
                    return literal[2]
                break
    return None


def _api_key() -> str:
    key = os.environ.get("AGENT_API_KEY", "").strip()
    if key:
        return key
    key = _shell_api_key()
    if key:
        return key

    path = _config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WpsError("换取令牌", f"请设置 AGENT_API_KEY 或在 {path} 配置 api_key") from None
    except OSError:
        raise WpsError("换取令牌", f"无法读取配置文件 {path}") from None
    except ValueError:
        raise WpsError("换取令牌", f"配置文件 {path} 应为 UTF-8 JSON") from None
    key = config.get("api_key") if isinstance(config, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise WpsError("换取令牌", f"配置文件 {path} 需要非空的 api_key 字符串")
    return key.strip()


class Client:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self._token = None
        self._renew_at = 0.0

    def _open(self, request, timeout, operation, *, write=False, json_errors=False):
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                status, headers = exc.code, exc.headers
                code = None
                if json_errors:
                    try:
                        code = _numeric_code(json.loads(exc.read(32768)))
                    except (ValueError, OSError):
                        pass
            finally:
                exc.close()
            kind = _http_kind(status, code)
            uncertain = write and status >= 500
            if uncertain:
                kind += "，写入结果待确认，请先回读后再决定是否重试"
            raise WpsError(operation, kind, status=status, code=code, headers=headers, uncertain=uncertain) from None
        except (OSError, ValueError):
            kind = "网络请求失败或超时"
            if write:
                kind += "，写入结果待确认，请先回读后再决定是否重试"
            raise WpsError(operation, kind, uncertain=write) from None

    def _json(self, url, method, timeout, operation, *, headers=None, body=None, write=False):
        headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, method=method, headers=headers,
                                         data=_encode(body) if body is not None else None)
        with self._open(request, timeout, operation, write=write, json_errors=True) as response:
            try:
                payload = json.load(response)
            except (ValueError, OSError, http.client.HTTPException):
                kind = "响应无法解析"
                if write:
                    kind += "，请先回读确认写入结果"
                raise WpsError(operation, kind, status=response.status, headers=response.headers, uncertain=write) from None
            if not isinstance(payload, dict):
                raise WpsError(operation, "响应应为 JSON 对象", status=response.status, uncertain=write)
            return payload, response.status, response.headers

    def _access_token(self, timeout):
        if self._token and time.monotonic() < self._renew_at:
            return self._token
        key = _api_key()
        started = time.monotonic()
        payload, status, headers = self._json(
            TOKEN_URL, "POST", min(timeout, 30), "换取令牌",
            body={"grant_type": "api_key", "api_key": key},
        )
        data = payload.get("data")
        token = data.get("access_token") if isinstance(data, dict) else None
        if payload.get("result") != "ok" or not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
            raise WpsError("换取令牌", "WPS 拒绝密钥或响应缺少有效令牌",
                           status=status, code=_numeric_code(payload), headers=headers)
        ttl = data.get("expires_in")
        self._token = token
        self._renew_at = started + (ttl - min(ttl / 10, 60) if type(ttl) is int and ttl > 0 else 0)
        return token

    def api(self, method, path, *, timeout=60, query=None, body=None, operation="WPS API",
            read_only=False, origin=API_ORIGIN):
        if (origin not in {API_ORIGIN, CONTENT_ORIGIN} or not path.startswith("/v7/")
                or any(char in path for char in "?#\\") or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("只接受固定 WPS 网关上的 V7 路径")
        method = method.upper()
        if origin == CONTENT_ORIGIN and (method != "GET" or not re.fullmatch(r"/v7/drives/[^/]+/files/[^/]+/content", path)):
            raise ValueError("备用网关只用于正文读取")
        read_only = read_only or method == "GET"
        url = origin + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
        for attempt in range(2):
            token = self._access_token(timeout)
            try:
                payload, status, headers = self._json(url, method, timeout, operation,
                    headers={"Authorization": f"Bearer {token}"}, body=body, write=not read_only)
                code = _numeric_code(payload)
                if code != 0:
                    kind = _http_kind(status, code) if code is not None else "响应缺少有效业务状态"
                    uncertain = not read_only and (code is None or str(code).startswith("5"))
                    if uncertain:
                        kind += "，请先回读确认写入结果"
                    raise WpsError(operation, kind, status=status, code=code, headers=headers, uncertain=uncertain)
                return payload.get("data") if payload.get("data") is not None else {}
            except WpsError as exc:
                if not (read_only and attempt == 0 and (exc.status == 401 or exc.code in {401, 401000001})):
                    raise
                self._token = None

    def transfer(self, url, method, *, timeout, headers=None, data=None, operation="文件传输"):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise WpsError(operation, "存储地址必须是 HTTPS")
        method = method.upper()
        if method not in {"GET", "PUT", "POST"}:
            raise WpsError(operation, "不支持的存储请求方法")
        request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
        return self._open(request, timeout, operation, write=method != "GET")

    def drive_transfer(self, url, method, *, timeout, headers=None, data=None, operation="云盘文件传输"):
        """WPS's own Drive storage gateway requires the current Agent access token."""
        headers = transfer_headers(headers)
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        wps_gateway = host.endswith(".ag.wps.cn") or host in {"openapi.wps.cn", "api.wps.cn"}
        if (wps_gateway and parsed.netloc.lower() == host and parsed.scheme == "https"
                and not any(key.lower() == "authorization" for key in headers)):
            headers["Authorization"] = f"Bearer {self._access_token(timeout)}"
        return self.transfer(url, method, timeout=timeout, headers=headers, data=data, operation=operation)


client = Client()


def transfer_headers(headers) -> dict:
    """Accept the map and name/value list used by different WPS storage APIs."""
    if isinstance(headers, list):
        return {item["name"]: item["value"] for item in headers}
    return dict(headers or {})


def resolve_link(value, timeout=60) -> dict:
    data = client.api("GET", f"/v7/links/{link_id(value)}/meta", timeout=timeout, operation="解析分享链接")
    if not isinstance(data, dict) or not data.get("drive_id") or not data.get("file_id"):
        raise WpsError("解析分享链接", "响应缺少 drive_id 或 file_id")
    return data


def file_meta(drive_id, file_id, timeout=60) -> dict:
    return client.api("GET", f"/v7/drives/{segment(drive_id)}/files/{segment(file_id)}/meta",
                      timeout=timeout, operation="读取文件元信息")


def meta_by_id(file_id, timeout=60) -> dict:
    return client.api("GET", f"/v7/files/{segment(file_id)}/meta", timeout=timeout,
                      operation="按文件 ID 查元信息")


def drive_id_from_meta(meta):
    drive = meta.get("drive")
    value = meta.get("drive_id") or (drive.get("id") if isinstance(drive, dict) else drive)
    if value:
        return str(value)
    raise ValueError("元信息缺少 drive_id，请通过 --drive-id 指定")


def download_file(drive_id, file_id, destination, timeout) -> tuple[int, str, str]:
    operation = "下载文件原件"
    info = client.api("GET", f"/v7/drives/{segment(drive_id)}/files/{segment(file_id)}/download",
                      timeout=timeout, query={"with_hash": "true"}, operation=operation)
    url = info.get("url")
    if not isinstance(url, str) or not url:
        raise WpsError(operation, "响应缺少下载地址")
    declared = next((h.get("sum") for h in (info.get("hashes") or [])
                     if str(h.get("type")).lower() == "sha1"), "")
    digest = hashlib.sha1()
    size = 0
    temporary = None
    try:
        with client.drive_transfer(url, "GET", timeout=timeout, headers=info.get("headers"), operation=operation) as response:
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(destination)), delete=False) as output:
                temporary = output.name
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        actual = digest.hexdigest()
        if declared and declared.lower() != actual:
            raise WpsError(operation, "下载内容与云端 SHA-1 不一致")
        os.replace(temporary, destination)
        temporary = None
        return size, actual, declared
    finally:
        if temporary is not None:
            os.unlink(temporary)


def body_content(drive_id, file_id, fmt, timeout=60, **options) -> dict:
    path = f"/v7/drives/{segment(drive_id)}/files/{segment(file_id)}/content"
    args = dict(timeout=timeout, query={"format": fmt, **options}, operation="正文抽取")
    try:
        return client.api("GET", path, **args)
    except WpsError as exc:
        if exc.status not in {404, 405} or exc.code is not None:
            raise
    return client.api("GET", path, origin=CONTENT_ORIGIN, **args)


def run(main):
    try:
        main()
    except (WpsError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
