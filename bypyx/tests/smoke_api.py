# -*- coding: utf-8 -*-
"""api.py 冒烟测试：账号 / 建目录 / 上传 / 取元信息 / 区间校验 / 删除。"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bypyx import XpanAPI, human_size  # noqa: E402

api = XpanAPI()

u = api.uinfo()
print("账号:", u.get("baidu_name"), "| uk:", u.get("uk"), "| errno:", u.get("errno"))

quota, used = api.quota()
print("配额: %s / 已用 %s" % (human_size(quota), human_size(used)))

root = api.list_dir("/apps/bypy")
print("根目录条目:", [(i["server_filename"], i["isdir"]) for i in root])

local = "/tmp/bypyx_smoke.bin"
with open(local, "wb") as f:
    f.write(os.urandom(5 * 1024 * 1024 + 12345))  # 故意不是 4MB 整数倍，覆盖尾片
remote = "/apps/bypy/_bypyx_smoke/smoke.bin"

n = api.upload_file(local, remote, progress=lambda d, t, s: print("  分片 %d/%d %.1fMB/s" % (d, t, s / 1048576.0)))
print("上传分片数:", n)

info = api.stat(remote)
print("远端:", info["server_filename"], info["size"], "本地:", os.path.getsize(local))

# 区间校验：首、中、尾
size = os.path.getsize(local)
data = open(local, "rb").read()
ok = True
for a, b in [(0, 1023), (size // 2, size // 2 + 1023), (size - 1024, size - 1)]:
    got = api.read_range(info["fs_id"], a, b)
    same = hashlib.md5(got).hexdigest() == hashlib.md5(data[a:b + 1]).hexdigest()
    ok = ok and same
    print("  区间 %d-%d %s" % (a, b, "一致" if same else "不一致"))
print("校验:", "通过" if ok else "失败")

api.delete(["/apps/bypy/_bypyx_smoke"])
print("清理后根目录:", [(i["server_filename"], i["isdir"]) for i in api.list_dir("/apps/bypy")])
