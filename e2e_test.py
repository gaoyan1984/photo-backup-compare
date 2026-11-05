# -*- coding: utf-8 -*-
"""端到端 HTTP 联调：造数据 → 扫描 → 预演 → 实际复制 → 复查"""
import json, os, shutil, tempfile, time, urllib.request

BASE = "http://127.0.0.1:8765"
ROOT = tempfile.mkdtemp(prefix="pbt_e2e_")
SRC = os.path.join(ROOT, "A源盘", "照片")
D1 = os.path.join(ROOT, "B备份")
D2 = os.path.join(ROOT, "C备份")
for d in (SRC, D1, D2):
    os.makedirs(d)


def put(root, rel, n):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(bytes([n % 251]) * n)


def sha_f(p):
    import hashlib
    return hashlib.md5(open(p, "rb").read()).hexdigest()


put(SRC, "2023\\DSC_0001.jpg", 12000)
put(SRC, "2023\\DSC_0002.jpg", 15000)
put(SRC, "2024\\旅行\\海边 DSC_0100.jpg", 33000)
put(SRC, "2024\\旅行\\孩子 IMG_0200.jpg", 22000)
put(SRC, "视频\\VID_0001.mp4", 88000)
put(D1, "2023\\DSC_0001.jpg", 12000)                     # 一致
put(D1, "2024\\旅行\\海边 DSC_0100.jpg", 9000)            # 大小不同
put(D1, "旧\\OLD_0001.jpg", 5000)                        # 源盘没有
put(D2, "2023\\DSC_0001.jpg", 12000)                     # 一致
put(D2, "2023\\DSC_0002.jpg", 15000)                     # 一致

fails = []


def check(name, cond, extra=""):
    print(("  ✔ " if cond else "  ✘ ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def post(u, b):
    r = urllib.request.urlopen(urllib.request.Request(
        BASE + u, json.dumps(b).encode(), {"Content-Type": "application/json"}))
    return json.loads(r.read())


def get(u):
    return json.loads(urllib.request.urlopen(BASE + u).read())


def wait(jid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = get("/api/job?id=" + jid)
        if j["state"] != "running":
            return j
        time.sleep(0.4)
    raise TimeoutError(jid)


print("[A] 扫描")
job = post("/api/scan", {"src_root": SRC, "dst_roots": [D1, D2],
                        "exts": [".jpg", ".mp4"], "verify_hash": True})
j = wait(job["id"])
check("扫描任务完成", j["state"] == "done", j.get("error") or "")
res = j["result"]
t1, t2 = res["targets"]
check("B: 缺失 3", t1["summary"]["missing"] == 3, f"={t1['summary']['missing']}")
check("B: 一致 1", t1["summary"]["same"] == 1, f"={t1['summary']['same']}")
check("B: 大小不同 1", t1["summary"]["size_diff"] == 1)
check("B: 多余 1", t1["summary"]["extra"] == 1)
check("B: 缺失体积正确", t1["summary"]["missing_bytes"] == 15000 + 22000 + 88000)
check("C: 缺失 3", t2["summary"]["missing"] == 3, f"={t2['summary']['missing']}")
check("C: 一致 2", t2["summary"]["same"] == 2)
check("四类统计互斥不重复",
      t1["summary"]["same"] + t1["summary"]["size_diff"] + t1["summary"]["hash_diff"]
      + t1["summary"]["missing"] == res["src_stats"]["files"],
      f"{t1['summary']['same']}+{t1['summary']['size_diff']}+"
      f"{t1['summary']['hash_diff']}+{t1['summary']['missing']} vs {res['src_stats']['files']}")
check("报告已生成", os.path.isfile(t1["reports"][0]["csv"]) and os.path.isfile(t1["reports"][0]["html"]),
      str(t1["reports"]))
check("容量预检充足", t1["capacity"]["enough"] is True)

print("[B] 预演")
j = wait(post("/api/copy", {"src_root": SRC, "dst_root": D2,
                           "rels": None, "dry_run": True})["id"])
r = j["result"]
check("预演成功计数 3", r["ok"] == 3, f"ok={r['ok']}")
check("预演未写盘", not os.path.isfile(os.path.join(D2, "2024\\旅行\\海边 DSC_0100.jpg")))

print("[C] 实际复制（双备份盘）")
j = wait(post("/api/copy", {"src_root": SRC, "dst_root": D1,
                           "rels": None, "dry_run": False})["id"])
r = j["result"]
check("B 盘复制成功 3 个", r["ok"] == 3 and r["failed"] == 0,
      f"ok={r['ok']} failed={r['failed']}")
j = wait(post("/api/copy", {"src_root": SRC, "dst_root": D2,
                           "rels": None, "dry_run": False})["id"])
r2 = j["result"]
check("C 盘复制成功 3 个", r2["ok"] == 3 and r2["failed"] == 0,
      f"ok={r2['ok']} failed={r2['failed']}")
check("深层中文目录已建", os.path.isfile(os.path.join(D1, "2024\\旅行\\孩子 IMG_0200.jpg")))
check("文件名完全一致", os.path.isfile(os.path.join(D1, "视频\\VID_0001.mp4")))
check("两盘各自独立复制", os.path.isfile(os.path.join(D2, "视频\\VID_0001.mp4")))

print("[D] 复查")
job = post("/api/scan", {"src_root": SRC, "dst_roots": [D1, D2],
                        "exts": [".jpg", ".mp4"], "verify_hash": True})
res = wait(job["id"])["result"]
a, b = res["targets"]
check("B: 缺失归零", a["summary"]["missing"] == 0)
check("B: 仅剩大小不同 1", a["summary"]["size_diff"] == 1)
check("B: 多余 1 未被动过", a["summary"]["extra"] == 1 and os.path.isfile(os.path.join(D1, "旧\\OLD_0001.jpg")))
check("C: 一致数=源文件总数", b["summary"]["missing"] == 0 and b["summary"]["same"] == 5,
      f"同={b['summary']['same']} 缺={b['summary']['missing']}")
check("源盘文件未被改动",
      os.path.getsize(os.path.join(SRC, "2023\\DSC_0001.jpg")) == 12000
      and not any(f.endswith(".part_tmp") for _, _, fs in os.walk(SRC) for f in fs))

print("[E] 反向：查「备份盘有、源盘没有」")
put(D2, "仅在C盘\\孤本 DSC_9999.jpg", 41000)          # 只在 C 盘
put(D1, "仅在B盘\\老片 IMG_8888.jpg", 27000)          # 只在 B 盘
job = post("/api/scan", {"src_root": SRC, "dst_roots": [D1, D2],
                        "exts": [".jpg", ".mp4"], "verify_hash": False})
res = wait(job["id"])["result"]
a, b = res["targets"]
check("B: 多余 2 个", a["summary"]["extra"] == 2, f"={a['summary']['extra']}")
check("C: 多余 1 个", b["summary"]["extra"] == 1, f"={b['summary']['extra']}")
check("多余样本已传给前端",
      any("老片" in r["rel"] for r in a["sample_extra"]))
check("多余分组已统计", any(g["name"] == "仅在B盘" for g in a["extra_groups"]))
check("反向容量约束按源盘算",
      a["reverse_capacity"]["need"] == 27000 + 5000, f"={a['reverse_capacity']['need']}")

print("[F] 反向：预演不写源盘")
j = wait(post("/api/copy", {"src_root": D1, "dst_root": SRC, "reverse": True,
                           "rels": None, "dry_run": True,
                           "exts": [".jpg", ".mp4"]})["id"])
r = j["result"]
check("反向预演计划 2 个", r["ok"] == 2, f"ok={r['ok']}")
check("反向预演未写源盘", not os.path.isfile(os.path.join(SRC, "仅在B盘\\老片 IMG_8888.jpg")))
check("反向预演终点是源盘", r["dst_root"] == os.path.normpath(SRC), r["dst_root"])

print("[G] 反向：实际补回源盘")
j = wait(post("/api/copy", {"src_root": D1, "dst_root": SRC, "reverse": True,
                           "rels": None, "dry_run": False,
                           "exts": [".jpg", ".mp4"]})["id"])
r = j["result"]
check("反向实际复制 2 个", r["ok"] == 2 and r["failed"] == 0,
      f"ok={r['ok']} failed={r['failed']}")
check("中文子目录已建", os.path.isfile(os.path.join(SRC, "仅在B盘\\老片 IMG_8888.jpg")))
check("文件名一致", os.path.isfile(os.path.join(SRC, "旧\\OLD_0001.jpg")))
check("内容哈希一致",
      sha_f(os.path.join(SRC, "仅在B盘\\老片 IMG_8888.jpg"))
      == sha_f(os.path.join(D1, "仅在B盘\\老片 IMG_8888.jpg")))
check("备份盘原文件未被改动", os.path.isfile(os.path.join(D1, "仅在B盘\\老片 IMG_8888.jpg")))

print("[H] 双向都补齐后复查")
job = post("/api/scan", {"src_root": SRC, "dst_roots": [D1, D2],
                        "exts": [".jpg", ".mp4"], "verify_hash": True})
res = wait(job["id"])["result"]
a, b = res["targets"]
check("B: 缺失/多余都归零",
      a["summary"]["missing"] == 0 and a["summary"]["extra"] == 0,
      f"缺={a['summary']['missing']} 多={a['summary']['extra']}")
check("C: 仍差 1 个（未做反向补齐）", b["summary"]["extra"] == 1)
check("反向补齐后一致数正确", a["summary"]["same"] == 6,
      f"同={a['summary']['same']}（含1个大小不同）")

shutil.rmtree(ROOT, ignore_errors=True)
print("=" * 56)
print("失败项：" + (", ".join(fails) if fails else "无，全部通过"))