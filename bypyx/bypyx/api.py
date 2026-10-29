# -*- coding: utf-8 -*-
"""bypyx.api —— 百度网盘 xpan 协议层

只用官方新版 xpan 接口（precreate → superfile2 → create），不依赖 bypy 的任何
上传/下载实现，只复用它的授权文件（~/.bypy/bypy.json）与 app 凭据。

设计约束（都是实测踩出来的，别改）：
1. precreate 对新文件也会返回 return_type=1，且 block_list 不完整。
   信它会生成空文件或损坏文件，所以一律无视它，老实传全部分片。
2. create 建目录支持一次建多级，但不能从 /apps 开始逐级建（/apps 返回 errno 102）。
3. 列表接口返回的 md5 字段，对分片上传的文件另有一套算法，不能和本地 md5 直接比。
4. 下载被限速，大文件校验只能抽样（Range 请求首/中/尾）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Callable, Iterable, List, Optional, Tuple

import requests

SANDBOX = "/apps/bypy"

API_FILE = "https://pan.baidu.com/rest/2.0/xpan/file"
API_MULTI = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
API_NAS = "https://pan.baidu.com/rest/2.0/xpan/nas"
API_QUOTA = "https://pan.baidu.com/api/quota"  # xpan/file?method=quota 返回 errno=3 Unsupported
API_SUPER = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"

DEFAULT_TOKEN_PATH = "~/.bypy/bypy.json"
DEFAULT_SLICE = 4 * 1024 * 1024
UA = "netdisk;14.0.0;PC;PC-Windows;10.0.19045;WindowsBaiduYunGuanJia"

# 这些错误码重试没有意义：token 失效、无权限、目录不可写、接口不支持
FATAL_ERRNOS = {3, 110, 111, 6, 102, -6, -7, 31064, 31066, 31023}


class XpanError(Exception):
    """接口返回的非 0 errno，或连续重试仍失败。"""

    def __init__(self, message: str, errno: Optional[int] = None, payload: Optional[dict] = None):
        super().__init__(message)
        self.errno = errno
        self.payload = payload or {}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGB" % n


def default_credentials() -> Tuple[Optional[str], Optional[str]]:
    """取 app 凭据：优先环境变量，其次复用已安装的 bypy 里的常量。

    刻意不在代码里硬编码 SecretKey —— 那份凭据属于 bypy 项目，写进自己的
    仓库不合适。装了 bypy 就自动借用，没装就要求用户提供。
    """
    key = os.environ.get("BAIDU_API_KEY")
    secret = os.environ.get("BAIDU_API_SECRET")
    if key and secret:
        return key, secret
    try:  # bypy 已安装则复用它的应用凭据，这样老 token 也能刷新
        from bypy import const as _const  # type: ignore

        return _const.ApiKey, _const.SecretKey
    except Exception:  # noqa: BLE001
        return None, None


class Token:
    """bypy 兼容的 token 文件读写与刷新。"""

    def __init__(self, path: str = DEFAULT_TOKEN_PATH, auto_refresh: bool = True):
        self.path = os.path.expanduser(path)
        self.auto_refresh = auto_refresh
        self._data: dict = {}
        self._lock = threading.Lock()
        self.load()

    # ---------------------------------------------------------------- 读写
    def load(self) -> dict:
        if not os.path.exists(self.path):
            raise XpanError("找不到 token 文件 %s，请先用 bypy info 完成授权" % self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            self._data = json.load(f)
        if not self._data.get("access_token"):
            raise XpanError("%s 里没有 access_token" % self.path)
        return self._data

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    @property
    def access_token(self) -> str:
        return self._data.get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self._data.get("refresh_token", "")

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> str:
        with self._lock:
            key, secret = default_credentials()
            if not (key and secret):
                raise XpanError("需要 app 凭据才能刷新 token，请设置 BAIDU_API_KEY / BAIDU_API_SECRET")
            if not self.refresh_token:
                raise XpanError("token 文件里没有 refresh_token，请重新执行 bypy info 授权")
            r = requests.post(
                TOKEN_URL,
                params={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": key,
                    "client_secret": secret,
                },
                timeout=60,
            )
            j = r.json()
            if "access_token" not in j:
                raise XpanError("刷新 token 失败：%s" % json.dumps(j, ensure_ascii=False)[:200])
            # 百度刷新时会同时下发新的 refresh_token，必须一起存
            self._data["access_token"] = j["access_token"]
            if j.get("refresh_token"):
                self._data["refresh_token"] = j["refresh_token"]
            if j.get("expires_in"):
                self._data["expires_in"] = j["expires_in"]
            self.save()
            return self._data["access_token"]


class XpanAPI:
    """xpan 接口封装。线程安全（每个线程独立 Session）。"""

    def __init__(self, token: Optional[Token] = None, token_path: str = DEFAULT_TOKEN_PATH,
                 retries: int = 5, timeout: tuple = (15, 300)):
        self.token = token or Token(token_path)
        self.retries = retries
        self.timeout = timeout
        self._local = threading.local()

    # ---------------------------------------------------------------- 底层请求
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA})
            self._local.session = s
        return s

    def _call(self, url: str, method_name: str, data: Optional[dict] = None,
              params: Optional[dict] = None, tries: Optional[int] = None) -> dict:
        """统一 POST 表单调用，带 token 刷新与退避重试。"""
        tries = tries or self.retries
        last = ""
        for attempt in range(1, tries + 1):
            q = {"method": method_name, "access_token": self.token.access_token}
            if params:
                q.update(params)
            try:
                r = self.session.post(url, params=q, data=data or {}, timeout=self.timeout)
                j = r.json()
            except Exception as e:  # noqa: BLE001
                last = str(e)
            else:
                errno = j.get("errno", j.get("error_code", 0))
                if errno in (0, -8, 102):  # -8/102：已存在，视为成功
                    return j
                last = "errno=%s %s" % (errno, j.get("errmsg") or j.get("error_msg") or "")
                if errno in (110, 111, 6):
                    # token 失效：刷新一次后立刻重试，不占用重试次数
                    self.token.refresh()
                    continue
                if errno in FATAL_ERRNOS:
                    raise XpanError("接口 %s 失败：%s" % (method_name, last), errno, j)
            if attempt < tries:
                time.sleep(min(2 ** attempt, 30))
        raise XpanError("接口 %s 连续 %d 次失败：%s" % (method_name, tries, last))

    # ---------------------------------------------------------------- 账号
    def uinfo(self) -> dict:
        r = self.session.get(API_NAS, params={"method": "uinfo", "access_token": self.token.access_token},
                             timeout=30)
        return r.json()

    def quota(self) -> Tuple[int, int]:
        """返回 (总空间, 已用)。用 web 端 /api/quota，xpan 没有配额接口。"""
        r = self.session.get(API_QUOTA, params={"checkfree": 1, "checkexpire": 1,
                                                "access_token": self.token.access_token}, timeout=30)
        j = r.json()
        if j.get("errno") not in (0, None):
            raise XpanError("取配额失败：%s" % json.dumps(j, ensure_ascii=False)[:200], j.get("errno"), j)
        return int(j.get("total", 0)), int(j.get("used", 0))

    # ---------------------------------------------------------------- 目录与文件
    def list_dir(self, path: str, limit: int = 1000) -> List[dict]:
        """列目录，自动翻页。返回原始条目（含 server_filename/isdir/size/fs_id/md5）。"""
        out: List[dict] = []
        start = 0
        while True:
            j = self._call(API_FILE, "list", data={"dir": path, "start": start, "limit": limit, "order": "name"})
            items = j.get("list") or []
            out.extend(items)
            if len(items) < limit:
                break
            start += limit
        return out

    def exists(self, path: str) -> bool:
        parent, name = os.path.dirname(path) or "/", os.path.basename(path)
        for item in self.list_dir(parent):
            if item.get("server_filename") == name:
                return True
        return False

    def stat(self, path: str) -> Optional[dict]:
        parent, name = os.path.dirname(path) or "/", os.path.basename(path)
        for item in self.list_dir(parent):
            if item.get("server_filename") == name:
                return item
        return None

    def mkdir(self, path: str) -> None:
        """建目录。xpan 支持一次建多级，但不能从 /apps 开始逐级建，所以只建目标一次。"""
        path = path.rstrip("/")
        if not path or path in ("/", SANDBOX):
            return
        self._call(API_FILE, "create", data={"path": path, "isdir": 1, "size": 0, "rtype": 3})

    def delete(self, paths: Iterable[str]) -> None:
        paths = [p for p in paths if p]
        if not paths:
            return
        self._call(API_FILE, "filemanager", data={"async": 0, "filelist": json.dumps(paths)},
                   params={"opera": "delete"})

    def meta(self, path: str) -> dict:
        """取单个文件的元信息（大小、md5、fs_id）。"""
        j = self._call(API_FILE, "filemetas", data={"target": json.dumps([path]), "dlink": 0})
        info = (j.get("info") or [{}])[0]
        if info.get("errno"):
            raise XpanError("取元信息失败：%s" % info, info.get("errno"), info)
        return info

    # ---------------------------------------------------------------- 上传
    def _precreate(self, path: str, size: int, block_list: List[str]) -> str:
        j = self._call(API_FILE, "precreate", data={
            "path": path, "size": size, "isdir": 0,
            "autoinit": 1, "rtype": 3, "block_list": json.dumps(block_list),
        })
        # 无论 return_type 是几，都必须真传分片，见模块顶部约束 1
        uploadid = j.get("uploadid")
        if not uploadid:
            raise XpanError("precreate 未返回 uploadid：%s" % json.dumps(j, ensure_ascii=False)[:200])
        return uploadid

    def _upload_slice(self, path: str, uploadid: str, index: int, buf: bytes) -> None:
        q = {"method": "upload", "access_token": self.token.access_token, "type": "tmpfile",
             "path": path, "uploadid": uploadid, "partseq": str(index)}
        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(API_SUPER, params=q,
                                      files={"file": ("blob", buf, "application/octet-stream")},
                                      timeout=(30, 3600))
                j = r.json()
                if j.get("error_code") in (0, None) or "md5" in j:
                    return
                last = "error_code=%s %s" % (j.get("error_code"), j.get("error_msg", ""))
                if j.get("error_code") in FATAL_ERRNOS:
                    raise XpanError("分片 %d 被拒绝：%s" % (index, last), j.get("error_code"), j)
            except XpanError:
                raise
            except Exception as e:  # noqa: BLE001
                last = str(e)
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 30))
        raise XpanError("分片 %d 连续 %d 次失败：%s" % (index, self.retries, last))

    def _create_file(self, path: str, size: int, block_list: List[str], uploadid: str) -> dict:
        return self._call(API_FILE, "create", data={
            "path": path, "size": size, "isdir": 0,
            "rtype": 3, "uploadid": uploadid, "block_list": json.dumps(block_list),
        })

    def upload_file(self, local: str, remote: str, slice_size: int = DEFAULT_SLICE,
                    progress: Optional[Callable[[int, int, float], None]] = None,
                    jobs: int = 1) -> int:
        """上传单个文件，返回分片数。中断后重跑同一调用即可续传（服务端保 uploadid）。

        progress(已完成片数, 总片数, 累计字节/秒) 每片回调一次。
        """
        if not os.path.isfile(local):
            raise XpanError("本地文件不存在：%s" % local)
        size = os.path.getsize(local)
        total = max(1, (size + slice_size - 1) // slice_size)

        self.mkdir(os.path.dirname(remote.rstrip("/")))

        # 空文件没有分片可传：precreate 会直接返回空 block_list，跳过上传直接 create。
        # 若照常走一片"空分片"，接口不报错但文件根本不会创建（实测）。
        if size == 0:
            empty = [hashlib.md5(b"").hexdigest()]
            uploadid = self._precreate(remote, 0, empty)
            self._create_file(remote, 0, empty, uploadid)
            if progress:
                progress(1, 1, 0.0)
            return 0

        block_list: List[str] = []
        with open(local, "rb") as f:
            while True:
                buf = f.read(slice_size)
                if not buf:
                    break
                block_list.append(hashlib.md5(buf).hexdigest())
                if len(buf) < slice_size:
                    break
        if not block_list:
            block_list = [hashlib.md5(b"").hexdigest()]

        uploadid = self._precreate(remote, size, block_list)

        if jobs <= 1:
            with open(local, "rb") as f:
                done = 0
                t0 = time.time()
                for i in range(total):
                    f.seek(i * slice_size)
                    buf = f.read(min(slice_size, size - i * slice_size))
                    self._upload_slice(remote, uploadid, i, buf)
                    done += len(buf)
                    if progress:
                        progress(i + 1, total, done / max(time.time() - t0, 0.001))
        else:
            from concurrent.futures import ThreadPoolExecutor

            done = 0
            t0 = time.time()
            lock = threading.Lock()

            def one(i: int) -> None:
                nonlocal done
                with open(local, "rb") as fh:
                    fh.seek(i * slice_size)
                    buf = fh.read(min(slice_size, size - i * slice_size))
                self._upload_slice(remote, uploadid, i, buf)
                with lock:
                    done += len(buf)
                    if progress:
                        progress(min(i + 1, total), total, done / max(time.time() - t0, 0.001))

            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(one, range(total)))

        self._create_file(remote, size, block_list, uploadid)
        return total

    # ---------------------------------------------------------------- 下载
    def dlink_of(self, fs_id: int) -> str:
        j = self.session.get(API_MULTI, params={"method": "filemetas", "access_token": self.token.access_token,
                                                "fsids": json.dumps([fs_id]), "dlink": 1}, timeout=60).json()
        items = j.get("list") or []
        if not items or not items[0].get("dlink"):
            raise XpanError("取下载链接失败：%s" % json.dumps(j, ensure_ascii=False)[:200],
                            j.get("errno"), j)
        return items[0]["dlink"]

    def read_range(self, fs_id: int, start: int, end: int) -> bytes:
        """按字节区间读远端内容（校验用）。"""
        r = self.session.get(self.dlink_of(fs_id), params={"access_token": self.token.access_token},
                             headers={"User-Agent": "pan.baidu.com", "Range": "bytes=%d-%d" % (start, end)},
                             timeout=(15, 180))
        return r.content

    def download_file(self, remote: str, local: str,
                      progress: Optional[Callable[[int, int], None]] = None) -> int:
        """下载单个文件到本地，支持 .part 断点续传。返回字节数。

        dlink 必须带 access_token 且 User-Agent 为 pan.baidu.com，否则 403。
        """
        info = self.meta(remote)
        size = int(info.get("size", 0))
        fs_id = info.get("fs_id")
        if not fs_id:
            raise XpanError("取不到 %s 的 fs_id" % remote)

        parent = os.path.dirname(local)
        if parent:
            os.makedirs(parent, exist_ok=True)
        part = local + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have == size and size > 0:
            os.replace(part, local)
            return size

        headers = {"User-Agent": "pan.baidu.com"}
        if have:
            headers["Range"] = "bytes=%d-" % have
        r = self.session.get(self.dlink_of(fs_id),
                             params={"access_token": self.token.access_token},
                             headers=headers, stream=True, timeout=(15, 300))
        r.raise_for_status()
        mode = "ab"
        if have and r.status_code != 206:  # 服务端不认 Range，只能从头写
            have, mode = 0, "wb"

        with open(part, mode) as f:
            for chunk in r.iter_content(1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                have += len(chunk)
                if progress:
                    progress(have, size)
        if size and have != size:
            raise XpanError("下载不完整：%s / %s" % (have, size))
        os.replace(part, local)
        return have

    def verify_file(self, remote: str, local: str, samples: int = 3) -> bool:
        """抽样校验：首 / 中 / 尾各取一段比对，避免全量下载被限速。"""
        info = self.meta(remote)
        fs_id = int(info["fs_id"])
        size = os.path.getsize(local)
        if int(info.get("size", -1)) != size:
            return False
        chunk = min(1 << 20, max(size // (samples + 1), 1))
        points = [0]
        for i in range(1, samples):
            points.append(max(size * i // samples - chunk // 2, 0))
        points.append(max(size - chunk, 0))
        with open(local, "rb") as f:
            for start in sorted(set(points)):
                end = min(start + chunk - 1, size - 1)
                f.seek(start)
                mine = f.read(end - start + 1)
                theirs = self.read_range(fs_id, start, end)
                if hashlib.md5(mine).hexdigest() != hashlib.md5(theirs).hexdigest():
                    return False
        return True
