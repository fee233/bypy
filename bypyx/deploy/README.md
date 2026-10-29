# bypy-sync 部署说明（bypyx 版）

把 Ubuntu（VMware-Ubuntu）上的多个目录镜像同步到百度网盘 `/apps/bypy`。

**2026-10 起重要变化**：bypy 依赖的旧分片上传接口已被百度下线，所有 **>20MB** 的文件都传不上去（报 `Slice MD5 mismatch`，实际是 `31064 file is not authorized`）。因此上传与同步改由 **bypyx**（本仓库 `bypyx/`）承担，配置格式、授权文件、日志目录都保持兼容，`bypy-sync.sh` 变成一层转发。

## 目录结构

```
/www/wwwroot/shells/bypy-sync/
├── bypy-sync.sh          # 兼容入口，转发给 bypyx（宝塔任务仍可调它）
├── bypy-sync.conf        # 唯一需要改的文件：填要同步的目录
├── bypyx-src/            # bypyx 源码（pip install -e 到这里）
├── README.md             # 本文件
├── README.legacy.md      # 旧 bash 版说明与排查记录（bypy 仍可用时）
├── bypy-sync.legacy.sh   # 旧 bash 版主脚本（仅作参考，勿再使用）
└── xpan-upload.py        # 旧的单文件上传工具（已由 bypyx upload 取代）
```

## 安装（只做一次）

**一键（推荐，不需要克隆仓库）：**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/fee233/bypy/master/bypyx/deploy/install.sh)
```

脚本做的事：建 venv → 装 bypy（凭据与授权流程）→ 从 GitHub 直装 bypyx → 生成示例配置 → 校验授权。也可以先下载再执行，`bash install.sh /自定义venv路径` 可指定安装位置。

**手动等价写法**（脚本失败时排查用）：

```bash
python3 -m venv /opt/bypy-venv
/opt/bypy-venv/bin/pip install setuptools wheel        # 3.12+ 的 venv 默认不带
/opt/bypy-venv/bin/pip install bypy                    # 凭据来源 + 授权流程
/opt/bypy-venv/bin/pip install "bypyx @ git+https://github.com/fee233/bypy.git#subdirectory=bypyx"
/opt/bypy-venv/bin/bypy info                           # 授权，浏览器里登录后回填 code
```

`#subdirectory=bypyx` 表示 bypyx 是仓库的子目录包，pip 会自行下载打包，本机不需要 git。授权也可以从旧机拷 `~/.bypy/bypy.json` 免登录。

## 常用命令

```bash
bypyx                          # 跑全部任务
bypyx sync -t immich           # 只跑一个任务
bypyx sync -n                  # 演练：只打印计划，不改动
bypyx -l                       # 列出任务
bypyx quota                    # 容量
bypyx upload 本地文件 远端路径 --verify    # 单文件上传，支持 >2GB
bypyx verify 本地文件 远端路径             # 抽样校验首/中/尾

./bypy-sync.sh                 # 与 bypyx 等价（兼容保留）
```

## 宝塔计划任务

任务类型「Shell 脚本」，内容一行（两种写法等价，推荐前者）：

```bash
/opt/bypy-venv/bin/bypyx sync
# /bin/bash /www/wwwroot/shells/bypy-sync/bypy-sync.sh
```

首次全量建议手动跑一次，之后定时任务只处理增量会快很多。日志在 `/var/log/bypy-sync/bypyx-YYYYMMDD.log`，默认保留 7 天。

## 长任务怎么托管

SSH 断开会杀掉前台进程。全量同步这类长任务用 systemd 临时单元跑，不会中断：

```bash
systemd-run --unit=bypyx-sync --working-directory=/www/wwwroot/shells/bypy-sync \
    /opt/bypy-venv/bin/bypyx sync

systemctl is-active bypyx-sync          # 是否还在跑
journalctl -u bypyx-sync -f             # 实时输出
systemctl stop bypyx-sync               # 中断
```

## 排障

| 现象 | 处理 |
|---|---|
| `另一个同步任务正在运行` | 正常。上次没跑完，本次主动跳过（锁文件 `/tmp/bypy-sync.lock`） |
| `授权不可用` / `errno=111` | token 过期且无法刷新。重新 `bypy info` 授权 |
| 同步“卡住”很久没输出 | 多半在列远端目录或处理大文件。加 `-v` 看每个目录的进度 |
| 某个大文件反复失败 | 单独用 `bypyx upload 本地 远端 --verify` 跑，能看到具体错误 |
| 想确认远端与本地是否一致 | `bypyx verify 本地文件 /apps/bypy/xxx` |

更细的协议层约束（为什么不能信 `return_type`、为什么列的 md5 不能比等）见 `bypyx/README.md`。
