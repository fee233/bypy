# bypyx

百度网盘同步引擎，基于官方 **xpan** 接口。它是本仓库（fork 自 [houtianze/bypy](https://github.com/houtianze/bypy)）新增的独立包，**不改动上游 `bypy/` 一行代码**。

## 为什么要有它

2026 年 10 月起，bypy 依赖的旧 PCS 分片上传接口已被百度下线：

| 现象 | 真相 |
|---|---|
| 日志报 `Slice MD5 mismatch` | 误报。bypy 把所有错误都归成 md5 不符 |
| 实际响应 | `31064 file is not authorized` |
| 结论 | 所有 **>20MB** 的文件都传不上去，小文件正常 |

社区版 bypy 用 `method=upload&type=tmpfile` 这套老流程，代码里连 `precreate` 都没有，无法通过调参绕过。所以上传必须换新接口。顺带把 `bypy-sync.sh` 那套多目录镜像同步也搬进来，用一个 Python 包替代 bash 脚本 + 独立上传工具两套东西。

## 特性

- **上传不设 2GB 上限**：真分片（默认 4MB/片），单文件最大 20GB
- **断点续传**：文件传输中断后重跑，已传分片由服务端复用；下载用 `.part` 续传
- **文件级并发**：`WORKERS` 控制同时上传几个文件，单文件内分片串行（每个文件互不共享状态）
- **镜像同步**：`delete_extra=1` 时删掉"另一边多出来的"文件，规则与 bash 版一致
- **目录级剪枝**：`EXCLUDE_DIRS_<任务名>` 整棵跳过目录，不再逐个列（immich 的哈希目录靠这个才跑得动）
- **复用 bypy 的授权**：直接读 `~/.bypy/bypy.json`，token 过期自动 refresh，**不需要重新授权**
- **非阻塞锁**：上一次没跑完，本次直接跳过退出，适合高频定时任务

## 安装

一键（不需要克隆仓库，连 bypy 一起装好）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/fee233/bypy/master/bypyx/deploy/install.sh)
```

手动安装：

```bash
python3 -m venv /opt/bypy-venv
/opt/bypy-venv/bin/pip install setuptools wheel          # 3.12+ 的 venv 默认不带
/opt/bypy-venv/bin/pip install bypy                      # 凭据来源 + 授权流程
/opt/bypy-venv/bin/pip install "bypyx @ git+https://github.com/fee233/bypy.git#subdirectory=bypyx"
```

第三行直接从 GitHub 安装子目录包，本机不需要 git。开发时改用 `-e /path/to/bypy/bypyx` 可编辑安装。

授权（只需要一次，沿用 bypy 的方式）：

```bash
/opt/bypy-venv/bin/bypy info        # 首次会打印授权链接，登录后回填 code
```

没有 bypy 也可以，但需要自己提供 app 凭据（见下文"凭据"）。

## 用法

```bash
bypyx                      # 等价于 bypyx sync，按配置跑全部任务
bypyx sync -t immich       # 只跑某个任务
bypyx sync -n              # 演练：只打印计划，不改动任何东西
bypyx sync -l              # 列出任务
bypyx quota                # 容量
bypyx whoami               # 授权账号
bypyx upload 本地文件 远端路径 --verify   # 单文件上传（支持 >2GB）
bypyx download 远端路径 本地文件
bypyx verify 本地文件 远端路径            # 抽样校验首/中/尾
```

配置文件默认按顺序查找：`-c 指定` → `$BYPYX_CONFIG` → `./bypy-sync.conf` → `/www/wwwroot/shells/bypy-sync/bypy-sync.conf`。

### 宝塔计划任务

任务类型选「Shell 脚本」，一行：

```bash
/opt/bypy-venv/bin/bypyx sync
```

## 配置

格式与 `bypy-sync.sh` 完全兼容，原来的 conf 不用改。`TASKS` 每行 6 段：

```
"名称|方向|本地目录|远端目录|删除多余|文件过滤正则"
```

- `方向`：`up` 本地→网盘，`down` 网盘→本地
- `远端目录`：相对 `/apps/bypy`，不要带前缀
- `删除多余`：`1` = 镜像（危险），`0` = 只增不删
- `过滤正则`：`re.match` 语义（从头匹配），所以匹配扩展名要写 `.*\.sql\.gz$`

bypyx 新增的可选项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `WORKERS` | 3 | 同时上传的文件数。小文件多时调到 8 |
| `SLICE` | 4MB | 单个分片大小。超过 64MB 会被强制降为 4MB |
| `EXCLUDE_DIRS_<任务名>` | 空 | 按目录名整棵跳过，逗号分隔，如 `encoded-video,thumbs` |
| `TOKEN_PATH` | `$BYPY_HOME/.bypy/bypy.json` | 授权文件位置 |

`PROCESSES`、`VERIFY_UPLOAD`、`BYPY_BIN`、`NET_TIMEOUT` 属于 bash 版参数，bypyx 忽略。

## 实现里固化的坑

这些都是实测踩出来的，改代码前先读：

1. **`precreate` 对全新文件也会返回 `return_type=1`**（"可秒传"），且 `block_list` 不完整。信它会得到空文件或损坏文件，所以一律无视，老实传全部分片。
2. **0 字节文件不需要传任何分片**。若照常发一片"空分片"，接口不报错但文件根本不会创建。必须 `precreate` 后直接 `create`。
3. **列的 `md5` 字段不能和本地 md5 比**。分片上传的文件有一套自己的算法，直接比会误判成损坏。校验只能下载回读（用 `verify` 抽样）。
4. **上传会自动创建父目录**，可以一次建多级。所以只有"本地是空目录"时才需要显式 `create`；否则每个目录一次请求会把同步拖死（实测 3000 个目录光建目录就卡十几分钟）。
5. **`/apps` 不能重复创建**（返回 `errno=102`），建目录要从 `/apps/bypy/xxx` 这一级开始写。
6. **下载 dlink 必须带 `access_token` 且 `User-Agent: pan.baidu.com`**，否则 403。
7. **配额接口是 `/api/quota`**，`xpan/file?method=quota` 返回 `errno=3 Unsupported open api`。

## 凭据

`api.py` 刻意不硬编码 SecretKey —— 那份凭据属于 bypy 项目。取用顺序：

1. 环境变量 `BAIDU_API_KEY` / `BAIDU_API_SECRET`
2. 已安装的 bypy 里的常量（`from bypy import const`）

两者都没有时，读写文件都能用（只要 token 没过期），只有刷新 token 会报错。

## 测试

```bash
python3 bypyx/tests/test_offline.py     # 离线：用内存 fake 验证同步计划，不联网
python3 bypyx/tests/smoke_api.py        # 联网：账号、上传、区间校验、删除（需要授权和网络）
```

`test_offline.py` 覆盖：增量判断、镜像删除、过滤正则含 `|`、被排除目录不被误删、演练零改动、单文件失败不影响其它、反向拉取、非法配置报错、真实 conf 解析。

## 与上游 bypy 的关系

`bypy/` 目录原样保留，作为参考实现和协议对照。两者可以共存：bypy 的 `list/upload(small)/download` 仍能用，bypyx 不依赖它的任何实现，只借用授权文件与 app 凭据。

MIT 许可，保留上游 `LICENSE` 与版权声明。
