# -*- coding: utf-8 -*-
"""bypyx —— 基于百度网盘 xpan 接口的同步引擎

bypy 的旧 PCS 分片上传接口已被百度下线（31064 file is not authorized），
导致 >20MB 的文件全部失败。bypyx 重写协议层，只走官方 xpan 接口，
并顺带实现多目录镜像同步（原 bypy-sync.sh 的功能）。
"""

__version__ = "0.1.0"

from .api import SANDBOX, Token, XpanAPI, XpanError, human_size  # noqa: F401
from .config import Config, ConfigError, Task, load  # noqa: F401
from .engine import Syncer, SyncStats  # noqa: F401
