# -*- coding: utf-8 -*-
"""bypyx.config —— 直接读取 bypy-sync.sh 的配置文件

刻意不去 eval bash：配置文件是纯文本，只解析 KEY=VALUE 和 TASKS=(...)。
这样既保留了原有的 conf 格式（一行都不用改），也不会因为配置写错而执行任意代码。

TASKS 每行格式：
    "名称|方向|本地目录|远端目录|删除多余|文件过滤正则"

注意：过滤正则里本身含 `|`（如 `(?:^|/)`），所以必须用 split("|", 5) 切分，
只切前 5 个分隔符，剩下全部归正则。bash 版靠 IFS read 的"余下全给最后一个变量"
恰好实现同样效果，这里是显式保证。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class ConfigError(Exception):
    """配置文件语法或取值有问题。"""


# ---------------------------------------------------------------- 默认值
DEFAULTS: Dict[str, str] = {
    "BYPY_HOME": "",
    "TOKEN_PATH": "",
    "LOG_DIR": "/var/log/bypy-sync",
    "LOG_KEEP_DAYS": "7",
    "RETRY": "5",
    "VERBOSE": "0",
    "SLEEP_BETWEEN": "2",
    "TASK_TIMEOUT": "0",
    "VERIFY_UPLOAD": "0",
    "SLICE": "4MB",
    "WORKERS": "3",
    "LOCK_FILE": "/tmp/bypy-sync.lock",
}

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)I?B?\s*$", re.I)
_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_size(value, default: int = 0) -> int:
    """'2GB' / '4MB' / '1024' -> 字节数。无法解析时返回 default。"""
    if isinstance(value, int):
        return value
    if not value:
        return default
    m = _SIZE_RE.match(str(value))
    if not m:
        return default
    return int(float(m.group(1)) * _MULT.get(m.group(2).upper(), 1))


@dataclass
class Task:
    name: str
    direction: str
    local: str
    remote: str
    delete_extra: bool = False
    pattern: str = ""
    exclude_dirs: Tuple[str, ...] = ()
    _rx: Optional[re.Pattern] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.direction not in ("up", "down"):
            raise ConfigError("任务 %s 的方向必须是 up 或 down，实际是 %r" % (self.name, self.direction))
        if not self.local.startswith("/"):
            raise ConfigError("任务 %s 的本地目录必须是绝对路径：%r" % (self.name, self.local))
        if self.pattern:
            try:
                self._rx = re.compile(self.pattern)
            except re.error as e:
                raise ConfigError("任务 %s 的过滤正则无法编译（%s）：%r" % (self.name, e, self.pattern))

    def matches(self, rel: str) -> bool:
        """相对路径是否参与同步。沿用 bypy 的 re.match（从头匹配）语义。"""
        if self._rx is None:
            return True
        return bool(self._rx.match(rel))

    def skip_dir(self, name: str) -> bool:
        """目录名命中排除清单时整棵跳过（不遍历、不建、不删）。

        过滤正则只作用于文件，没法用来剪枝；而 immich 的 thumbs 是两层哈希目录，
        逐个目录列一次列表就是几千次请求。这个开关专门解决遍历成本。
        """
        return name in self.exclude_dirs


@dataclass
class Config:
    path: str
    settings: Dict[str, str]
    tasks: List[Task]

    # ------------------------------------------------------------ 取值辅助
    def get(self, key: str, default: str = "") -> str:
        return self.settings.get(key, DEFAULTS.get(key, default))

    def num(self, key: str, default: int = 0) -> int:
        try:
            return int(str(self.get(key)).strip() or default)
        except ValueError:
            return default

    def size(self, key: str, default: int = 0) -> int:
        """取带单位的配置，如 SLICE=4MB。"""
        raw = str(self.get(key)).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return parse_size(raw, default)

    def flag(self, key: str) -> bool:
        return str(self.get(key)).strip() in ("1", "true", "True", "yes", "on")

    @property
    def token_path(self) -> str:
        if self.get("TOKEN_PATH"):
            return os.path.expanduser(self.get("TOKEN_PATH"))
        home = self.get("BYPY_HOME").strip()
        if home:
            return os.path.join(os.path.expanduser(home), ".bypy", "bypy.json")
        return os.path.expanduser("~/.bypy/bypy.json")

    @property
    def lock_file(self) -> str:
        return self.get("LOCK_FILE", "/tmp/bypy-sync.lock")

    @property
    def log_dir(self) -> str:
        return self.get("LOG_DIR", "/var/log/bypy-sync")

    # ------------------------------------------------------------ 任务查找
    def pick(self, names: Optional[List[str]]) -> List[Task]:
        if not names:
            return list(self.tasks)
        known = {t.name: t for t in self.tasks}
        missing = [n for n in names if n not in known]
        if missing:
            raise ConfigError("配置里没有这些任务：%s（现有：%s）"
                              % ("、".join(missing), "、".join(known) or "无"))
        return [known[n] for n in names]


# ---------------------------------------------------------------- 解析
def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留引号内的 #。"""
    out, quote = [], ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out)


def _unquote(raw: str) -> str:
    raw = raw.strip().rstrip(",").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        body = raw[1:-1]
        if raw[0] == "'":
            return body
        return (body.replace("\\\\", "\\").replace('\\"', '"').replace("\\$", "$"))
    return raw


def _extract_tasks(lines: List[str]) -> Tuple[List[str], List[str]]:
    """把 TASKS=(...) 整块摘出来，返回 (任务行, 其余行)。"""
    task_lines: List[str] = []
    rest: List[str] = []
    i = 0
    seen = False
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not seen and re.match(r"^TASKS\s*=\s*\(", s):
            seen = True
            tail = s.split("(", 1)[1]
            if ")" in tail:  # 同一行写完，罕见但支持
                body = tail.split(")", 1)[0]
                if body.strip():
                    task_lines.append(body)
                i += 1
                continue
            i += 1
            while i < len(lines) and lines[i].strip() != ")":
                task_lines.append(lines[i])
                i += 1
            i += 1
            continue
        rest.append(line)
        i += 1
    return task_lines, rest


def _parse_task_line(line: str) -> Optional[Task]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    body = _unquote(s)
    if not body:
        return None
    parts = [p.strip() for p in body.split("|", 5)]
    while len(parts) < 6:
        parts.append("")
    name, direction, local, remote, delete_extra, pattern = parts
    if not name:
        return None
    return Task(
        name=name,
        direction=direction.lower(),
        local=local.rstrip("/") or "/",
        remote=remote.strip("/"),
        delete_extra=delete_extra == "1",
        pattern=pattern,
    )


def load(path: str) -> Config:
    if not os.path.exists(path):
        raise ConfigError("配置文件不存在：%s" % path)
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    task_lines, rest = _extract_tasks(lines)

    settings: Dict[str, str] = dict(DEFAULTS)
    for line in rest:
        s = _strip_comment(line).strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        settings[key] = _unquote(value.strip())

    tasks: List[Task] = []
    for line in task_lines:
        t = _parse_task_line(line)
        if t:
            tasks.append(t)

    # 目录级排除：EXCLUDE_DIRS_<任务名> 优先，其次全局 EXCLUDE_DIRS
    for t in tasks:
        raw = settings.get("EXCLUDE_DIRS_" + t.name, settings.get("EXCLUDE_DIRS", ""))
        t.exclude_dirs = tuple(n.strip() for n in re.split(r"[,;\s]+", raw) if n.strip())

    if not tasks:
        raise ConfigError("%s 里没有解析到任何任务，请检查 TASKS=(...) 写法" % path)

    names = [t.name for t in tasks]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ConfigError("任务名重复：%s" % "、".join(sorted(dup)))

    return Config(path=path, settings=settings, tasks=tasks)
