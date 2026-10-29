# -*- coding: utf-8 -*-
"""bypyx 命令行入口

    bypyx sync [-c 配置] [-t 任务名] [-n] [-l]
    bypyx upload 本地文件 远端路径 [--verify] [--jobs N]
    bypyx download 远端路径 本地文件
    bypyx verify 本地文件 远端路径
    bypyx quota | whoami | list
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional

from . import __version__
from .api import SANDBOX, XpanAPI, XpanError, human_size
from .config import Config, ConfigError, load
from .engine import Syncer, SyncStats

log = logging.getLogger("bypyx")

DEFAULT_CONFIGS = [
    os.environ.get("BYPYX_CONFIG", ""),
    "./bypy-sync.conf",
    "/www/wwwroot/shells/bypy-sync/bypy-sync.conf",
]


def find_config(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    for p in DEFAULT_CONFIGS:
        if p and os.path.exists(p):
            return p
    raise ConfigError("找不到配置文件，请用 -c 指定（默认查找 ./bypy-sync.conf）")


def setup_logging(log_dir: str, keep_days: int, verbose: bool) -> str:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    logfile = ""
    try:
        os.makedirs(log_dir, exist_ok=True)
        logfile = os.path.join(log_dir, "bypyx-%s.log" % datetime.now().strftime("%Y%m%d"))
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        _clean_old_logs(log_dir, keep_days)
    except OSError as e:
        log.warning("无法写入日志目录 %s：%s（只输出到终端）" % (log_dir, e))
    return logfile


def _clean_old_logs(log_dir: str, keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = datetime.now().timestamp() - keep_days * 86400
    try:
        for name in os.listdir(log_dir):
            if not name.startswith("bypy"):
                continue
            p = os.path.join(log_dir, name)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
    except OSError:
        pass


def take_lock(path: str):
    """拿非阻塞锁，拿不到返回 None（让上层安静退出，不算失败）。"""
    try:
        fh = open(path, "w")
    except OSError:
        return True  # 锁文件不可写时不阻塞任务
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def print_tasks(cfg: Config) -> None:
    print("配置文件：%s" % cfg.path)
    print("授权文件：%s" % cfg.token_path)
    print("任务数：%d\n" % len(cfg.tasks))
    for i, t in enumerate(cfg.tasks, 1):
        print("[%d] %s" % (i, t.name))
        print("    方向    %s" % ("本地 → 网盘" if t.direction == "up" else "网盘 → 本地"))
        print("    本地    %s" % t.local)
        print("    远端    %s/%s" % (SANDBOX, t.remote or ""))
        print("    镜像    %s" % ("是（删多余）" if t.delete_extra else "否（只增不删）"))
        if t.pattern:
            print("    过滤    %s" % t.pattern)


def print_summary(results) -> int:
    if not results:
        return 0
    print()
    header = "任务                 方向   结果        上传    下载    跳过    删除   失败   耗时"
    print(header)
    print("-" * len(header))
    failed = 0
    for task, st in results:
        if not st.ok:
            failed += 1
        print("%-20s %-6s %-11s %-7d %-7d %-7d %-6d %-6d %s"
              % (task.name, task.direction.upper(), st.status, st.uploaded, st.downloaded,
                 st.skipped, st.deleted, st.failed, "%ds" % int(st.elapsed)))
        for err in st.errors[:3]:
            print("    ! %s" % err)
    print()
    print("完成：%d 个任务，%d 个失败" % (len(results), failed))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- 子命令
def cmd_sync(args) -> int:
    cfg = load(find_config(args.config))
    if args.list:
        print_tasks(cfg)
        return 0

    verbose = args.verbose or cfg.flag("VERBOSE")
    setup_logging(cfg.log_dir, cfg.num("LOG_KEEP_DAYS", 7), verbose)

    lock = None
    if not args.dry_run:
        lock = take_lock(cfg.lock_file)
        if lock is None:
            log.info("另一个同步任务正在运行（%s），本次跳过" % cfg.lock_file)
            return 0

    tasks = cfg.pick(args.task)
    log.info("bypyx %s 开始，%d 个任务%s" % (__version__, len(tasks), "（演练）" if args.dry_run else ""))
    log.info("配置：%s" % cfg.path)
    try:
        api = XpanAPI(token_path=cfg.token_path)
    except XpanError as e:
        log.error("授权不可用：%s" % e)
        return 2

    results = Syncer(api, cfg, dry_run=args.dry_run).run(tasks)
    code = print_summary(results)
    log.info("结束，日志：%s" % os.path.join(cfg.log_dir, "bypyx-%s.log" % datetime.now().strftime("%Y%m%d")))
    if lock not in (None, True):
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return code


def _api_from(args) -> XpanAPI:
    cfg = load(find_config(args.config))
    setup_logging(cfg.log_dir, cfg.num("LOG_KEEP_DAYS", 7), args.verbose)
    return XpanAPI(token_path=cfg.token_path)


def cmd_upload(args) -> int:
    api = _api_from(args)
    local = os.path.abspath(os.path.expanduser(args.local))
    if not os.path.isfile(local):
        print("本地文件不存在：%s" % local, file=sys.stderr)
        return 2
    remote = args.remote
    if not remote.startswith("/"):
        remote = SANDBOX + "/" + remote.lstrip("/")
    size = os.path.getsize(local)
    print("上传 %s（%s）→ %s" % (local, human_size(size), remote))

    t0 = __import__("time").time()
    n = api.upload_file(local, remote, jobs=args.jobs,
                        progress=lambda d, t, s: print("  分片 %d/%d  %.1fMB/s" % (d, t, s / 1048576.0)))
    cost = __import__("time").time() - t0
    print("完成：%d 片，%d 秒，%.1fMB/s" % (n, cost, size / max(cost, 0.001) / 1048576.0))

    if args.verify:
        ok = api.verify_file(remote, local)
        print("抽样校验：%s" % ("通过" if ok else "失败"))
        return 0 if ok else 1
    return 0


def cmd_download(args) -> int:
    api = _api_from(args)
    remote = args.remote
    if not remote.startswith("/"):
        remote = SANDBOX + "/" + remote.lstrip("/")
    got = api.download_file(remote, os.path.abspath(os.path.expanduser(args.local)))
    print("已下载 %s" % human_size(got))
    return 0


def cmd_verify(args) -> int:
    api = _api_from(args)
    local = os.path.abspath(os.path.expanduser(args.local))
    remote = args.remote
    if not remote.startswith("/"):
        remote = SANDBOX + "/" + remote.lstrip("/")
    ok = api.verify_file(remote, local, samples=args.samples)
    print("抽样校验：%s" % ("通过" if ok else "不一致"))
    return 0 if ok else 1


def cmd_quota(args) -> int:
    api = _api_from(args)
    total, used = api.quota()
    print("总空间 %s，已用 %s，剩余 %s" % (human_size(total), human_size(used),
                                          human_size(max(total - used, 0))))
    return 0


def cmd_whoami(args) -> int:
    api = _api_from(args)
    u = api.uinfo()
    print("账号：%s  uk=%s  vip=%s" % (u.get("baidu_name"), u.get("uk"), u.get("vip_type")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bypyx", description="百度网盘同步（xpan 接口）")
    p.add_argument("-V", "--version", action="version", version="bypyx %s" % __version__)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("-c", "--config", help="配置文件路径")
        sp.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("sync", help="按配置同步（默认命令）")
    common(s)
    s.add_argument("-t", "--task", action="append", help="只跑指定任务，可重复")
    s.add_argument("-n", "--dry-run", action="store_true", help="演练，不实际改动")
    s.add_argument("-l", "--list", action="store_true", help="只列出任务")
    s.set_defaults(func=cmd_sync)

    u = sub.add_parser("upload", help="上传单个文件（支持 >2GB）")
    common(u)
    u.add_argument("local")
    u.add_argument("remote")
    u.add_argument("--verify", action="store_true", help="上传后抽样校验")
    u.add_argument("--jobs", type=int, default=1, help="单文件内并发分片数")
    u.set_defaults(func=cmd_upload)

    d = sub.add_parser("download", help="下载单个文件")
    common(d)
    d.add_argument("remote")
    d.add_argument("local")
    d.set_defaults(func=cmd_download)

    v = sub.add_parser("verify", help="抽样校验远端与本地是否一致")
    common(v)
    v.add_argument("local")
    v.add_argument("remote")
    v.add_argument("--samples", type=int, default=3)
    v.set_defaults(func=cmd_verify)

    q = sub.add_parser("quota", help="查看容量")
    common(q)
    q.set_defaults(func=cmd_quota)

    w = sub.add_parser("whoami", help="查看授权账号")
    common(w)
    w.set_defaults(func=cmd_whoami)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 无子命令时默认按 sync 处理，方便宝塔计划任务只写一行；
    # 但 -h/-V 这类全局开关不能被塞到 sync 后面，否则会被子命令拒掉
    if not argv:
        argv = ["sync"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help", "-V", "--version"):
        argv = ["sync"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except ConfigError as e:
        print("配置错误：%s" % e, file=sys.stderr)
        return 2
    except XpanError as e:
        print("接口错误：%s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
