# -*- coding: utf-8 -*-
"""离线测试：不联网，用内存 fake 模拟 xpan 接口，验证同步计划是否符合预期。

    python3 bypyx/tests/test_offline.py
"""

import os
import sys
import tempfile
import types

# 本地没装 requests 时给个空壳，api.py 只是 import 它，离线路径不会用到
try:
    import requests  # noqa: F401
except ImportError:
    stub = types.ModuleType("requests")

    class _Session:
        def __init__(self, *a, **k):
            self.headers = {}

        def post(self, *a, **k):
            raise RuntimeError("离线测试不应发起网络请求")

        def get(self, *a, **k):
            raise RuntimeError("离线测试不应发起网络请求")

    stub.Session = _Session
    stub.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("离线测试不应联网"))
    sys.modules["requests"] = stub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bypyx.config import ConfigError, load  # noqa: E402
from bypyx.engine import Syncer  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s %s" % (name, detail))


class FakeAPI:
    """内存版远端：path -> {'size': n, 'isdir': bool}"""

    def __init__(self, tree=None, fail_on=()):
        self.tree = dict(tree or {})
        self.uploads = []
        self.deletes = []
        self.mkdirs = []
        self.downloads = []
        self.fail_on = set(fail_on)

    def list_dir(self, path):
        from bypyx.api import XpanError

        prefix = path.rstrip("/") + "/"
        children = {}
        for p, meta in self.tree.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest:
                top = rest.split("/")[0]
                children.setdefault(top, {"isdir": True})
            else:
                children[rest] = meta
        if not children and not any(p == path for p in self.tree):
            raise XpanError("目录不存在：%s" % path, -9)
        out = []
        for name, meta in children.items():
            item = {"server_filename": name, "isdir": bool(meta.get("isdir"))}
            if not item["isdir"]:
                item["size"] = meta["size"]
                item["fs_id"] = abs(hash(prefix + name)) % 10 ** 9
            out.append(item)
        return out

    def mkdir(self, path):
        self.mkdirs.append(path)
        self.tree.setdefault(path, {"isdir": True})

    def upload_file(self, local, remote, slice_size=0, progress=None, jobs=1):
        if remote in self.fail_on:
            raise RuntimeError("模拟上传失败")
        self.uploads.append(remote)
        self.tree[remote] = {"size": os.path.getsize(local)}

    def delete(self, paths):
        for p in paths:
            self.deletes.append(p)
            for k in [k for k in self.tree if k == p or k.startswith(p + "/")]:
                self.tree.pop(k, None)

    def meta(self, remote):
        m = self.tree.get(remote)
        if not m:
            raise RuntimeError("远端不存在：%s" % remote)
        return {"size": m["size"], "fs_id": abs(hash(remote)) % 10 ** 9}

    def download_file(self, remote, local, progress=None):
        self.downloads.append(remote)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(b"x" * self.tree[remote]["size"])
        return self.tree[remote]["size"]


def write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"a" * size)
    return path


CONF_TMPL = """\
LOG_DIR={log}
RETRY=1
WORKERS=1
SLEEP_BETWEEN=0
TASKS=(
  "site|up|{local}|site|1|"
)
"""


def make_conf(tmp, local, extra_task=""):
    conf = os.path.join(tmp, "bypy-sync.conf")
    body = CONF_TMPL.format(log=os.path.join(tmp, "log"), local=local)
    if extra_task:
        body = body.replace("\n)", "\n" + extra_task + "\n)")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(body)
    return conf


def case_basic_up():
    print("\n[1] 增量上传 + 跳过未变 + 镜像删除")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    local = os.path.join(tmp, "site")
    write(os.path.join(local, "new.txt"), 10)
    write(os.path.join(local, "same.txt"), 20)
    write(os.path.join(local, "changed.txt"), 30)
    write(os.path.join(local, "sub/keep.txt"), 5)
    os.makedirs(os.path.join(local, "emptydir"), exist_ok=True)

    api = FakeAPI({
        "/apps/bypy/site/same.txt": {"size": 20},
        "/apps/bypy/site/changed.txt": {"size": 11},
        "/apps/bypy/site/gone.txt": {"size": 7},
    })
    cfg = load(make_conf(tmp, local))
    syncer = Syncer(api, cfg)

    st = syncer.run_task(cfg.tasks[0])
    up = sorted(p.replace("/apps/bypy/site/", "") for p in api.uploads)
    check("状态 OK", st.status == "OK", st.status)
    check("上传缺失与变化的三处（含子目录文件）", up == ["changed.txt", "new.txt", "sub/keep.txt"], up)
    check("同大小文件跳过", st.skipped == 1, st.skipped)
    check("镜像删掉远端多余文件", api.deletes == ["/apps/bypy/site/gone.txt"], api.deletes)
    check("只补空目录（有文件的目录由上传自动创建）",
          api.mkdirs == ["/apps/bypy/site/emptydir"], api.mkdirs)
    check("统计计数", (st.uploaded, st.failed) == (3, 0), (st.uploaded, st.failed))


def case_filter():
    print("\n[2] 过滤正则含竖线，且不能误删被排除的目录")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    local = os.path.join(tmp, "immich")
    write(os.path.join(local, "_data/library/a.jpg"), 5)
    write(os.path.join(local, "_data/thumbs/t.jpg"), 5)
    write(os.path.join(local, "_data/encoded-video/v.mp4"), 5)

    api = FakeAPI({
        "/apps/bypy/immich/_data/library/a.jpg": {"size": 5},   # 已同步
        "/apps/bypy/immich/_data/thumbs/t.jpg": {"size": 5},    # 被排除，远端不该动
        "/apps/bypy/immich/_data/encoded-video/v.mp4": {"size": 5},
        "/apps/bypy/immich/_data/thumbs/old.jpg": {"size": 5},  # 被排除，绝不能删
    })
    task_line = ('  "immich|up|%s|immich|1|'
                 '^(?!.*(?:^|/)(encoded-video|thumbs)(/|$)).*$"' % local)
    cfg = load(make_conf(tmp, local, task_line))
    task = [t for t in cfg.tasks if t.name == "immich"][0]
    check("正则里的 | 没被切坏", "(encoded-video|thumbs)" in task.pattern, task.pattern)

    st = Syncer(api, cfg).run_task(task)
    check("被排除的目录不上传", api.uploads == [], api.uploads)
    check("被排除的文件不被删除", api.deletes == [], api.deletes)
    check("没有失败", st.status == "OK", st.status)


def case_dry_run():
    print("\n[3] 演练不产生任何改动")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    local = os.path.join(tmp, "site")
    write(os.path.join(local, "a.txt"), 3)
    api = FakeAPI({"/apps/bypy/site/gone.txt": {"size": 1}})
    cfg = load(make_conf(tmp, local))
    st = Syncer(api, cfg, dry_run=True).run_task(cfg.tasks[0])
    check("状态是 DRY", st.status == "DRY", st.status)
    check("没有上传", api.uploads == [], api.uploads)
    check("没有删除", api.deletes == [], api.deletes)
    check("没有建目录", api.mkdirs == [], api.mkdirs)
    check("预告了待办数量", st.uploaded == 1 and st.deleted == 1, (st.uploaded, st.deleted))


def case_failure():
    print("\n[4] 单个文件失败要计入错误且不影响其它文件")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    local = os.path.join(tmp, "site")
    write(os.path.join(local, "bad.txt"), 4)
    write(os.path.join(local, "good.txt"), 4)
    api = FakeAPI({}, fail_on={"/apps/bypy/site/bad.txt"})
    cfg = load(make_conf(tmp, local))
    st = Syncer(api, cfg).run_task(cfg.tasks[0])
    check("状态 FAIL", st.status == "FAIL", st.status)
    check("失败计数 1", st.failed == 1, st.failed)
    check("另一个文件仍然传成功", "/apps/bypy/site/good.txt" in api.uploads, api.uploads)


def case_down():
    print("\n[5] 反向拉取")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    local = os.path.join(tmp, "pulled")
    os.makedirs(local)
    write(os.path.join(local, "same.txt"), 6)
    api = FakeAPI({
        "/apps/bypy/site/same.txt": {"size": 6},
        "/apps/bypy/site/new.txt": {"size": 8},
    })
    conf = os.path.join(tmp, "bypy-sync.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write('LOG_DIR=%s\nRETRY=1\nTASKS=(\n  "pull|down|%s|site|0|"\n)\n'
                % (os.path.join(tmp, "log"), local))
    cfg = load(conf)
    st = Syncer(api, cfg).run_task(cfg.tasks[0])
    check("只下缺失的文件", [p.replace("/apps/bypy/site/", "") for p in api.downloads] == ["new.txt"],
          api.downloads)
    check("已存在的跳过", st.skipped == 1, st.skipped)


def case_bad_config():
    print("\n[6] 配置错误要报出来而不是静默跳过")
    tmp = tempfile.mkdtemp(prefix="bypyx-t-")
    conf = os.path.join(tmp, "bad.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write('TASKS=(\n  "x|sideways|/tmp|r|0|"\n)\n')
    try:
        load(conf)
        check("方向非法被拒绝", False)
    except ConfigError as e:
        check("方向非法被拒绝", "up 或 down" in str(e), str(e))

    with open(conf, "w", encoding="utf-8") as f:
        f.write('TASKS=(\n  "x|up|relative/path|r|0|"\n)\n')
    try:
        load(conf)
        check("相对路径被拒绝", False)
    except ConfigError as e:
        check("相对路径被拒绝", "绝对路径" in str(e), str(e))


def case_parse_real():
    print("\n[7] 现有服务器配置能原样解析")
    conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "bypy-sync.conf")
    if not os.path.exists(conf):
        check("跳过（无 fixture）", True)
        return
    cfg = load(conf)
    names = [t.name for t in cfg.tasks]
    check("任务数 3", len(cfg.tasks) == 3, names)
    immich = [t for t in cfg.tasks if t.name == "immich"][0]
    check("immich 正则完整",
          immich.pattern == r"^(?!.*(?:^|/)(encoded-video|thumbs)(/|$)).*$", immich.pattern)
    check("immich 镜像开启", immich.delete_extra)
    check("token 路径跟随 BYPY_HOME", cfg.token_path == "/root/.bypy/bypy.json", cfg.token_path)
    check("SLICE=2GB 解析正确", cfg.size("SLICE", 0) == 2 * 1024 ** 3, cfg.size("SLICE", 0))


if __name__ == "__main__":
    case_basic_up()
    case_filter()
    case_dry_run()
    case_failure()
    case_down()
    case_bad_config()
    case_parse_real()
    print("\n%d 通过，%d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
