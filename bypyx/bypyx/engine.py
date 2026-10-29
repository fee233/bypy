# -*- coding: utf-8 -*-
"""bypyx.engine —— 同步引擎

与 bash 版 bypy-sync.sh 的语义保持一致：

  up   : 本地 → 网盘，按大小判断是否需要上传（远端缺失或大小不同就传）
  down : 网盘 → 本地，规则同上反过来
  镜像 : delete_extra=1 时删掉"另一边多出来的"文件

几处刻意的设计（都是上一版踩过坑的地方）：

1. 过滤正则只作用于**文件**，不作用于目录。否则像 `.*\\.sql\\.gz$` 这种只匹配
   文件的正则会把所有目录都判为不匹配，整棵树都走不进去。
2. 带过滤正则的任务**不删除远端多余目录**。否则远端一个"全是被排除文件"的目录
   会被判定为多余，进而被递归删除 —— 那正是我们想保留的 9.6G 视频缓存。
3. 上传是文件级并发，单文件内部按 4MB 切片串行。bypy 的多进程之所以写坏数据是
   因为它共享一个 hashcache 文件；这里的每个文件互不共享状态。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .api import SANDBOX, XpanAPI, XpanError, human_size
from .config import Config, Task

log = logging.getLogger("bypyx")


@dataclass
class SyncStats:
    direction: str = ""
    uploaded: int = 0
    downloaded: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    moved: int = 0
    dirs: int = 0
    status: str = "OK"
    elapsed: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "DRY")


class TaskTimeout(Exception):
    pass


class Syncer:
    def __init__(self, api: XpanAPI, cfg: Config, dry_run: bool = False):
        self.api = api
        self.cfg = cfg
        self.dry_run = dry_run
        self.workers = max(1, cfg.num("WORKERS", 3))
        self.retry = max(1, cfg.num("RETRY", 5))
        self.timeout = cfg.num("TASK_TIMEOUT", 0)
        self.slice = cfg.size("SLICE", 4 * 1024 * 1024) or 4 * 1024 * 1024
        # 老配置里的 SLICE=2GB 是给 bypy 单次上传用的，bypyx 是真分片，太大会慢
        if self.slice > 64 * 1024 * 1024:
            self.slice = 4 * 1024 * 1024
        self._deadline = 0.0
        self._lock_errors: List[str] = []
        self._progress_lock = threading.Lock()

    # ------------------------------------------------------------ 汇总入口
    def run(self, tasks: List[Task]) -> List[Tuple[Task, SyncStats]]:
        results: List[Tuple[Task, SyncStats]] = []
        for idx, task in enumerate(tasks):
            if idx and self.cfg.num("SLEEP_BETWEEN", 0):
                time.sleep(self.cfg.num("SLEEP_BETWEEN", 0))
            log.info("▶ [%s] %s %s → %s/%s" % (task.name, task.direction.upper(),
                                               task.local, SANDBOX, task.remote or ""))
            st = self.run_task(task)
            results.append((task, st))
            log.info("  [%s] 完成：%s%s" % (task.name, st.status,
                                           ("，" + st.errors[0]) if st.errors else ""))
        return results

    def run_task(self, task: Task) -> SyncStats:
        t0 = time.time()
        st = SyncStats(direction=task.direction)
        self._deadline = (t0 + self.timeout) if self.timeout > 0 else 0.0
        try:
            if task.direction == "up":
                if not os.path.isdir(task.local):
                    st.status = "MISSING"
                    st.errors.append("本地目录不存在：%s" % task.local)
                    log.error("  ✗ 本地目录不存在：%s，跳过" % task.local)
                    return st
                self._sync_up(task, st)
            else:
                self._sync_down(task, st)
        except TaskTimeout:
            st.status = "TIMEOUT"
            st.errors.append("超过 TASK_TIMEOUT=%ds" % self.timeout)
        except XpanError as e:
            st.status = "FAIL"
            st.errors.append(str(e))
            log.error("  ✗ %s" % e)
        except Exception as e:  # noqa: BLE001
            st.status = "FAIL"
            st.errors.append("%s: %s" % (type(e).__name__, e))
            log.exception("  ✗ 未预期的错误")
        finally:
            st.elapsed = time.time() - t0
        if st.failed:
            st.status = "FAIL" if st.status == "OK" else st.status
        if self.dry_run and st.status == "OK":
            st.status = "DRY"
        return st

    # ------------------------------------------------------------ 遍历
    @staticmethod
    def _walk_local(root: str, task: "Task") -> Tuple[Dict[str, int], Set[str]]:
        files: Dict[str, int] = {}
        dirs: Set[str] = set()
        keep = task.matches
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir:
                dirs.add(rel_dir)
            # 在遍历阶段剪枝：删掉 dirnames 里的项，os.walk 就不会往下走
            if task.exclude_dirs:
                dirnames[:] = [d for d in dirnames if not task.skip_dir(d)]
            for name in filenames:
                rel = name if not rel_dir else rel_dir + "/" + name
                if not keep(rel):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    files[rel] = os.path.getsize(path)
                except OSError as e:
                    log.warning("  跳过无法读取的文件 %s：%s" % (rel, e))
        return files, dirs

    def _walk_remote(self, root: str, task: "Task"
                     ) -> Tuple[Dict[str, dict], Set[str]]:
        files: Dict[str, dict] = {}
        dirs: Set[str] = set()
        keep = task.matches
        queue: List[str] = [""]
        while queue:
            rel = queue.pop()
            cur = root if not rel else root + "/" + rel
            try:
                items = self.api.list_dir(cur)
            except XpanError as e:
                if e.errno in (-9, -8):  # 目录不存在
                    continue
                raise
            for it in items:
                name = it.get("server_filename")
                if not name:
                    continue
                child = name if not rel else rel + "/" + name
                if it.get("isdir"):
                    if task.skip_dir(name):
                        continue  # 整棵跳过：既不遍历也不参与删除
                    dirs.add(child)
                    queue.append(child)
                elif keep(child):
                    files[child] = it
            log.debug("    列目录 %s：%d 项（累计文件 %d）"
                      % (rel or "/", len(items), len(files)))
        return files, dirs

    @staticmethod
    def _remote_root(task: Task) -> str:
        if not task.remote:
            return SANDBOX
        return SANDBOX + "/" + task.remote

    # ------------------------------------------------------------ 上传
    def _sync_up(self, task: Task, st: SyncStats) -> None:
        root = self._remote_root(task)
        local_files, local_dirs = self._walk_local(task.local, task)
        remote_files, remote_dirs = self._walk_remote(root, task)

        todo: List[str] = []
        for rel, size in local_files.items():
            item = remote_files.get(rel)
            if item is None or int(item.get("size", -1)) != size:
                todo.append(rel)
            else:
                st.skipped += 1
        todo.sort()

        extra_files = [r for r in remote_files if r not in local_files] if task.delete_extra else []
        extra_dirs: List[str] = []
        if task.delete_extra and not task.pattern:
            extra_dirs = sorted((d for d in remote_dirs if d not in local_dirs),
                                key=lambda d: (-d.count("/"), d))

        total = sum(local_files[r] for r in todo)
        log.info("  本地 %d 个文件 / 远端 %d 个 → 待传 %d（%s），跳过 %d，待删 %d"
                 % (len(local_files), len(remote_files), len(todo), human_size(total),
                    st.skipped, len(extra_files) + len(extra_dirs)))

        if self.dry_run:
            for rel in todo[:10]:
                log.info("    [演练] 上传 %s（%s）" % (rel, human_size(local_files[rel])))
            if len(todo) > 10:
                log.info("    [演练] …… 其余 %d 个" % (len(todo) - 10))
            for rel in extra_files[:5]:
                log.info("    [演练] 删除远端文件 %s" % rel)
            st.uploaded = len(todo)
            st.deleted = len(extra_files) + len(extra_dirs)
            return

        # 目录：xpan 上传时会自动补齐父目录（实测可一次建多级），
        # 所以只需要补"本地是空目录"的那些，否则每个目录一次请求会把同步拖死。
        if local_dirs:
            with_files: Set[str] = set()
            for rel in local_files:
                parts = rel.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    with_files.add("/".join(parts[:i]))
            empties = [d for d in (local_dirs - with_files) if d not in remote_dirs]
            if empties:
                parents = {os.path.dirname(d) for d in empties}
                # 父目录会被 create 一并建好，只发叶子目录，省一半请求
                empties = [d for d in empties if d not in parents]
                st.dirs += self._mkdir_many(root, empties)

        if todo:
            self._upload_many(task, root, todo, local_files, st)

        if extra_files:
            self._delete_remote(root, extra_files, st)
        if extra_dirs:
            self._delete_remote_dirs(root, extra_dirs, st)

    def _mkdir_many(self, root: str, dirs: List[str]) -> int:
        """并发补建空目录，返回成功数。"""
        done = 0

        def one(d: str) -> bool:
            try:
                self.api.mkdir(root + "/" + d)
                return True
            except XpanError as e:
                log.warning("  建目录失败 %s：%s" % (d, e))
                return False

        with ThreadPoolExecutor(max_workers=min(8, len(dirs))) as pool:
            for ok in pool.map(one, dirs):
                done += 1 if ok else 0
        log.info("  补建空目录 %d 个" % done)
        return done

    def _upload_many(self, task: Task, root: str, todo: List[str],
                     local_files: Dict[str, int], st: SyncStats) -> None:
        total = len(todo)
        progress = {"done": 0, "bytes": 0}

        def tick(size: int) -> None:
            with self._progress_lock:
                progress["done"] += 1
                progress["bytes"] += size
                n = progress["done"]
            if n % 50 == 0 or n == total:
                log.info("  进度 %d/%d，已传 %s" % (n, total, human_size(progress["bytes"])))

        def one(rel: str) -> None:
            if self._deadline_passed():
                raise TaskTimeout()
            local_abs = os.path.join(task.local, rel)
            remote_abs = root + "/" + rel
            size = local_files[rel]
            last: Optional[Exception] = None
            for attempt in range(1, self.retry + 1):
                try:
                    t0 = time.time()
                    self.api.upload_file(local_abs, remote_abs, slice_size=self.slice,
                                         progress=self._progress_cb(rel, size))
                    cost = time.time() - t0
                    st.uploaded += 1
                    if size >= 8 * 1024 * 1024:
                        log.info("    ↑ %s %s  %.1fMB/s" % (rel, human_size(size),
                                                            size / max(cost, 0.001) / 1048576))
                    else:
                        log.debug("    ↑ %s %s" % (rel, human_size(size)))
                    tick(size)
                    return
                except TaskTimeout:
                    raise
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt < self.retry:
                        wait = min(2 ** attempt, 30)
                        log.warning("    上传重试 %d/%d %s：%s（%ds 后重试）"
                                    % (attempt, self.retry, rel, e, wait))
                        time.sleep(wait)
            st.failed += 1
            msg = "%s：%s" % (rel, last)
            st.errors.append(msg)
            log.error("    ✗ %s" % msg)

        if self.workers == 1 or len(todo) == 1:
            for rel in todo:
                if self._deadline_passed():
                    st.errors.append("超时，剩余 %d 个未传" % len(todo))
                    st.failed += 1
                    break
                try:
                    one(rel)
                except TaskTimeout:
                    st.errors.append("超时")
                    break
            return

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(one, rel): rel for rel in todo}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except TaskTimeout:
                    pool.shutdown(wait=False, cancel_futures=True)
                    st.errors.append("超时")
                    break

    def _progress_cb(self, rel: str, size: int):
        if size < 32 * 1024 * 1024:
            return None
        state = {"pct": 0}

        def cb(done: int, total: int, speed: float) -> None:
            pct = int(done * 100 / max(total, 1))
            if pct >= state["pct"] + 20 or done == total:
                state["pct"] = pct
                log.info("      %s %d%%（%s / %.1fMB/s）"
                         % (rel, pct, human_size(size * pct / 100.0), speed / 1048576.0))

        return cb

    def _delete_remote(self, root: str, rels: List[str], st: SyncStats) -> None:
        batch = [root + "/" + r for r in rels]
        for i in range(0, len(batch), 100):
            chunk = batch[i:i + 100]
            try:
                self.api.delete(chunk)
                st.deleted += len(chunk)
                log.info("    − 删除远端 %d 个多余文件" % len(chunk))
            except XpanError as e:
                st.failed += 1
                st.errors.append("删除失败：%s" % e)
                log.error("    删除远端失败：%s" % e)

    def _delete_remote_dirs(self, root: str, dirs: List[str], st: SyncStats) -> None:
        for d in dirs:  # 已经按深度倒序，先删子目录
            try:
                self.api.delete([root + "/" + d])
                st.deleted += 1
                log.info("    − 删除远端多余目录 %s" % d)
            except XpanError as e:
                log.warning("    删除目录失败 %s：%s" % (d, e))

    # ------------------------------------------------------------ 下载
    def _sync_down(self, task: Task, st: SyncStats) -> None:
        root = self._remote_root(task)
        remote_files, _remote_dirs = self._walk_remote(root, task)
        local_files = {}
        if os.path.isdir(task.local):
            local_files, _dirs = self._walk_local(task.local, task)

        todo: List[str] = []
        for rel, item in remote_files.items():
            size = int(item.get("size", -1))
            if local_files.get(rel) != size:
                todo.append(rel)
            else:
                st.skipped += 1
        todo.sort()

        extra = [r for r in local_files if r not in remote_files] if task.delete_extra else []
        total = sum(int(remote_files[r].get("size", 0)) for r in todo)
        log.info("  远端 %d 个文件 / 本地 %d 个 → 待下 %d（%s），跳过 %d，待删 %d"
                 % (len(remote_files), len(local_files), len(todo), human_size(total),
                    st.skipped, len(extra)))

        if self.dry_run:
            for rel in todo[:10]:
                log.info("    [演练] 下载 %s" % rel)
            st.downloaded = len(todo)
            st.deleted = len(extra)
            return

        for rel in todo:
            if self._deadline_passed():
                st.status = "TIMEOUT"
                st.errors.append("超时，剩余未下载")
                break
            local_abs = os.path.join(task.local, rel)
            try:
                self.api.download_file(root + "/" + rel, local_abs)
                st.downloaded += 1
                log.debug("    ↓ %s" % rel)
            except Exception as e:  # noqa: BLE001
                st.failed += 1
                st.errors.append("%s：%s" % (rel, e))
                log.error("    ✗ 下载失败 %s：%s" % (rel, e))

        for rel in extra:
            try:
                os.unlink(os.path.join(task.local, rel))
                st.deleted += 1
                log.info("    − 删除本地多余文件 %s" % rel)
            except OSError as e:
                log.warning("    删除本地失败 %s：%s" % (rel, e))

    # ------------------------------------------------------------ 杂项
    def _deadline_passed(self) -> bool:
        return bool(self._deadline) and time.time() > self._deadline
