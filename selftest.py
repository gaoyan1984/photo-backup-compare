# -*- coding: utf-8 -*-
"""自测：在临时目录里模拟 源盘/备份盘，跑通 全流程并断言结果"""
import os, shutil, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

BASE = tempfile.mkdtemp(prefix="pbt_test_")
SRC = os.path.join(BASE, "A_photos")
B1 = os.path.join(BASE, "B_backup")
B2 = os.path.join(BASE, "C_backup")
os.makedirs(SRC); os.makedirs(B1); os.makedirs(B2)


def put(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def sha(p):
    import hashlib
    return hashlib.md5(open(p, "rb").read()).hexdigest()


PH = b"\xff\xd8\xff\xe0" + b"X" * 3000          # 假 jpg
PH2 = b"\xff\xd8\xff\xe0" + b"Y" * 5000

# 源盘：4 个文件，分两层目录 + 含空格/中文的文件名
put(SRC, "2023\\DSC_0001.jpg", PH)
put(SRC, "2023\\DSC_0002.jpg", PH)
put(SRC, "2023\\子目录\\风景 DSC_0010.jpg", PH2)
put(SRC, "视频\\VID_0001.mp4", PH * 2)

# B 盘：1 个正确 + 1 个内容不同(大小完全相同) + 1 个大小不同 + 1 个多余
put(B1, "2023\\DSC_0001.jpg", PH)
put(B1, "2023\\子目录\\风景 DSC_0010.jpg", b"\xff\xd8\xff\xe0" + b"Z" * 5000)  # 字节数相同、内容不同
put(B1, "2023\\DSC_0002.jpg", PH[:1000])          # 大小不同
put(B1, "旧照片\\OLD_0001.jpg", PH)              # 源盘没有

# C 盘：3 个正确 + 目录留空
put(B2, "2023\\DSC_0001.jpg", PH)
put(B2, "2023\\DSC_0002.jpg", PH)
put(B2, "视频\\VID_0001.mp4", PH * 2)

print("=" * 60)
fails = []


def check(name, cond, extra=""):
    print(("  ✔ " if cond else "  ✘ ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


print("[1] 扫描")
s = engine.scan_tree(SRC, engine.DEFAULT_EXTS)
check("源盘找到 4 个文件", s["stats"]["files"] == 4, f"实得 {s['stats']['files']}")
check("目录层级保留", engine.norm_key("2023\\子目录\\风景 DSC_0010.jpg") in s["files"])
check("空格+中文文件名正确",
      {r["rel"] for r in s["files"].values()} == {
          "2023\\DSC_0001.jpg", "2023\\DSC_0002.jpg",
          "2023\\子目录\\风景 DSC_0010.jpg", "视频\\VID_0001.mp4"})
check("大小写归一化后键唯一", len(s["files"]) == 4)

d1 = engine.scan_tree(B1, engine.DEFAULT_EXTS)
d2 = engine.scan_tree(B2, engine.DEFAULT_EXTS)

print("[2] 比对 B 盘（开 MD5 校验）")
c1 = engine.compare(s["files"], d1["files"], verify_hash=True)
check("B 缺失 1 个（视频）", len(c1["missing"]) == 1, f"={[r['rel'] for r in c1['missing']]}")
check("B 大小不同 1 个", len(c1["size_diff"]) == 1, f"={[r['rel'] for r in c1['size_diff']]}")
check("B 内容不同 1 个（MD5）", len(c1["hash_diff"]) == 1, f"={[r['rel'] for r in c1['hash_diff']]}")
check("B 一致 1 个", len(c1["same"]) == 1)
check("B 多余 1 个", len(c1["extra"]) == 1)

print("[3] 比对 C 盘")
c2 = engine.compare(s["files"], d2["files"], verify_hash=False)
check("C 缺失 1 个（风景）", len(c2["missing"]) == 1, f"={[r['rel'] for r in c2['missing']]}")
check("C 一致 3 个", len(c2["same"]) == 3)

print("[4] 容量预检")
cap = engine.check_capacity(sum(r["size"] for r in c2["missing"]), B2)
check("容量判定充足", cap["enough"] is True, f"需 {engine.human(cap['need'])} 剩 {engine.human(cap['free'])}")

print("[5] 预演（dry_run）")
before = engine.scan_tree(B2, engine.DEFAULT_EXTS)["stats"]["files"]
r = engine.copy_missing(c2["missing"], SRC, B2, dry_run=True)
after = engine.scan_tree(B2, engine.DEFAULT_EXTS)["stats"]["files"]
check("预演未真的写盘", before == after, f"{before} → {after}")
check("预演计划数正确", r["ok"] == 1)

print("[6] 实际复制到 B")
r1 = engine.copy_missing(c1["missing"], SRC, B1, dry_run=False)
check("复制成功 1 个", r1["ok"] == 1, str({k: v for k, v in r1.items() if k != "log"}))
newp = os.path.join(B1, "视频\\VID_0001.mp4")
check("目录结构与文件名一致", os.path.isfile(newp))
check("内容哈希一致", os.path.isfile(newp) and sha(newp) == sha(os.path.join(SRC, "视频\\VID_0001.mp4")))
check("修改时间保留", os.path.isfile(newp) and abs(os.path.getmtime(newp) - os.path.getmtime(os.path.join(SRC, "视频\\VID_0001.mp4"))) < 2)
check("无临时残留 .part_tmp", not any(f.endswith(".part_tmp") for _, _, fs in os.walk(B1) for f in fs))

print("[7] 实际复制到 C")
r2 = engine.copy_missing(c2["missing"], SRC, B2, dry_run=False)
check("复制成功 1 个", r2["ok"] == 1)

print("[8] 二次扫描：应完全一致")
s2 = engine.scan_tree(SRC, engine.DEFAULT_EXTS)
b2b = engine.scan_tree(B2, engine.DEFAULT_EXTS)
cc = engine.compare(s2["files"], b2b["files"], verify_hash=True)
check("C 盘现在零缺失", len(cc["missing"]) == 0)
check("C 盘现在全部一致", len(cc["same"]) == 4, f"同={len(cc['same'])} 缺={len(cc['missing'])} "
      f"大小差={len(cc['size_diff'])} 内容差={len(cc['hash_diff'])}")
b1b = engine.scan_tree(B1, engine.DEFAULT_EXTS)
c3 = engine.compare(s2["files"], b1b["files"], verify_hash=True)
check("B 盘缺失已补上", len(c3["missing"]) == 0)
check("B 盘仍标记出 2 处不一致", len(c3["size_diff"]) + len(c3["hash_diff"]) == 2)

print("[9] 重复复制应全部跳过（不重复写）")
rr = engine.copy_missing(c2["missing"], SRC, B2, dry_run=False)
check("已存在→跳过", rr["skipped"] == 1 and rr["ok"] == 0)

print("[10] 路径越界防护")
evil = [{"rel": "..\\..\\逃逸.jpg", "src_path": os.path.join(SRC, "2023\\DSC_0001.jpg"), "size": 3}]
er = engine.copy_missing(evil, SRC, B2, dry_run=False)
check("越界路径被阻止", er["failed"] == 1, str(er["log"]))
check("逃逸文件未生成", not os.path.exists(os.path.join(BASE, "逃逸.jpg")))

print("[11] 报告导出")
rows = engine.build_report_rows(c2, SRC, B2, "C")
csv_path = os.path.join(BASE, "r.csv")
engine.write_csv(csv_path, rows)
html_path = os.path.join(BASE, "r.html")
summ = engine.summarize(c2, s["stats"], engine.scan_tree(B2, engine.DEFAULT_EXTS)["stats"])
with open(html_path, "w", encoding="utf-8") as f:
    f.write(engine.build_html_report(c2, summ, SRC, B2, "C"))
check("CSV 有内容", os.path.getsize(csv_path) > 100, f"{os.path.getsize(csv_path)} 字节")
check("HTML 有内容", os.path.getsize(html_path) > 500, f"{os.path.getsize(html_path)} 字节")
check("CSV 带 BOM（Excel 不乱码）", open(csv_path, "rb").read(3) == b"\xef\xbb\xbf")

print("[12] 空目录 / 不存在目录")
e1 = engine.scan_tree(os.path.join(BASE, "不存在的目录"), engine.DEFAULT_EXTS)
check("不存在目录不崩溃", e1["stats"]["files"] == 0)
e2 = engine.compare(s["files"], e1["files"])
check("备份盘为空→全部缺失", len(e2["missing"]) == 4)

print("[13] 扫描空盘后自动建目录")
newdst = os.path.join(BASE, "D_new")
os.makedirs(newdst)
e3 = engine.scan_tree(newdst, engine.DEFAULT_EXTS)
c4 = engine.compare(s["files"], e3["files"])
r4 = engine.copy_missing(c4["missing"], SRC, newdst, dry_run=False)
check("空盘全量复制 4 个", r4["ok"] == 4 and r4["failed"] == 0, str({k: v for k, v in r4.items() if k != "log"}))
check("深层目录已创建", os.path.isfile(os.path.join(newdst, "2023\\子目录\\风景 DSC_0010.jpg")))

print("[14] 反向对称性：A 的 extra 必须等于 B 的 missing")
REV = os.path.join(BASE, "E_备份盘")         # 扮演备份盘
SHARED = os.path.join(BASE, "F_源盘")        # 扮演源盘
os.makedirs(REV); os.makedirs(SHARED)
put(SHARED, "共有\\C1.jpg", b"C" * 1000)     # 两边都有
put(REV, "独有\\R1.jpg", b"R" * 2000)        # 只有备份盘有
put(REV, "共有\\C1.jpg", b"C" * 1000)

rx = engine.scan_tree(REV, engine.DEFAULT_EXTS)      # 备份盘索引
sx = engine.scan_tree(SHARED, engine.DEFAULT_EXTS)   # 源盘索引
# 备份盘当源 = 「反向补齐」视角：找出源盘缺的
as_backup = engine.compare(rx["files"], sx["files"])
# 源盘当源 = 「正向补齐」视角：找出备份盘缺的
as_source = engine.compare(sx["files"], rx["files"])

check("备份盘当源：源盘缺 1（独有照片）",
      len(as_backup["missing"]) == 1 and
      as_backup["missing"][0]["rel"] == "独有\\R1.jpg",
      f"={[r['rel'] for r in as_backup['missing']]}")
check("源盘当源：备份盘不缺", len(as_source["missing"]) == 0,
      f"={[r['rel'] for r in as_source['missing']]}")
check("核心对称性：A的extra == B的missing",
      {r["rel"].casefold() for r in as_source["extra"]} ==
      {r["rel"].casefold() for r in as_backup["missing"]} ==
      {"独有\\r1.jpg"})
check("反向视角 extra 为空", len(as_backup["extra"]) == 0)
check("共有文件双向都判为一致",
      len(as_backup["same"]) == 1 and len(as_source["same"]) == 1)

# 反向复制：把独有照片补回源盘
rr = engine.copy_missing(as_backup["missing"], REV, SHARED, dry_run=False)
check("反向复制成功", rr["ok"] == 1 and rr["failed"] == 0)
back = os.path.join(SHARED, "独有\\R1.jpg")
check("反向复制落点路径一致", os.path.isfile(back))
check("反向复制内容一致", sha(back) == sha(os.path.join(REV, "独有\\R1.jpg")))
# 必须重新扫描，sx 是复制前的旧索引
sx2 = engine.scan_tree(SHARED, engine.DEFAULT_EXTS)
check("反向复制后源盘不缺了",
      len(engine.compare(rx["files"], sx2["files"])["missing"]) == 0)
check("反向复制后备份盘也不缺了",
      len(engine.compare(sx2["files"], rx["files"])["missing"]) == 0)
check("原始备份盘文件仍在（是复制不是移动）",
      os.path.isfile(os.path.join(REV, "独有\\R1.jpg")))

shutil.rmtree(BASE, ignore_errors=True)
print("=" * 60)
print("失败项：" + (", ".join(fails) if fails else "无，全部通过"))
sys.exit(1 if fails else 0)