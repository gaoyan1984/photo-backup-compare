# -*- coding: utf-8 -*-
"""
照片备份对比与同步引擎（纯标准库，无需第三方依赖）

核心能力：
1. scan_tree   —— 扫描某个根目录下的所有文件，生成 路径->(大小,修改时间) 的索引
2. compare     —— 用「相对路径」为键，把源盘和备份盘做逐项比对
3. copy_missing—— 只复制「备份盘缺失」的文件，保持相对路径与文件名完全一致
4. 报告导出    —— CSV（Excel 可直接打开）+ HTML（可读性强的汇总报告）

安全原则：
- 全程只做「读取 + 复制」，不删除、不移动、不改写源盘任何文件
- 目标盘已存在的文件一律跳过，除非用户显式勾选「覆盖」
- 复制后按字节大小二次核验，核验不通过记入失败清单
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import time

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

DEFAULT_EXTS = [
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".eps",
    # 视频
    ".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".3gp", ".3g2",
    ".insv", ".mpg", ".mpeg", ".wmv", ".asf", ".flv", ".f4v", ".m4v",
    ".vob", ".rm", ".rmvb", ".webm", ".ts",
]

# 扫描时无条件跳过的系统/垃圾目录（不区分大小写）
IGNORE_DIRS = {
    "$recycle.bin", "system volume information", "windows.old",
    "$windows.~bt", "$windows.~ws", "recovery",
    "node_modules", ".git", ".svn", "__pycache__",
    ".thumbnails", ".cache", ".trash", ".trashes", ".ds_store",
}


# --------------------------------------------------------------------------
# 数据结构（用普通 dict，方便直接 JSON 序列化给网页）
# --------------------------------------------------------------------------

def make_record(rel: str, path: str, size: int, mtime: float) -> dict:
    return {"rel": rel, "path": path, "size": size, "mtime": mtime}


def norm_key(rel: str) -> str:
    """比对用的键：统一分隔符 + 忽略大小写（Windows 文件系统本身不区分大小写）"""
    return rel.replace("/", "\\").strip("\\").casefold()


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


# --------------------------------------------------------------------------
# 1. 扫描
# --------------------------------------------------------------------------

def scan_tree(root, exts=None, ignore_dirs=None, on_progress=None,
              cancel=None, follow_symlinks=False):
    """
    递归扫描 root，返回：
        files  : {key: record}   key 为 norm_key(相对路径)
        errors : [ {path, error} ]
        stats  : {dirs, files, bytes, elapsed, cancelled}
    exts = None 或空列表 表示「所有文件类型」
    """
    root = os.path.normpath(os.path.abspath(root))
    ignore = set(IGNORE_DIRS)
    if ignore_dirs:
        ignore |= {d.strip().casefold() for d in ignore_dirs if d.strip()}

    ext_set = None
    if exts:
        ext_set = {e.strip().casefold() if e.strip().startswith(".") else "." + e.strip().casefold()
                   for e in exts if e.strip()}

    files, errors = {}, []
    stack = [root]
    n_dirs = n_files = n_bytes = 0
    t0 = time.time()
    last_push = 0.0
    cancelled = False

    while stack:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        cur = stack.pop()
        n_dirs += 1
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        name = entry.name
                        if name.casefold() in ignore:
                            continue
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=follow_symlinks):
                            continue
                        if ext_set is not None and os.path.splitext(name)[1].casefold() not in ext_set:
                            continue
                        st = entry.stat()
                        rel = os.path.relpath(entry.path, root)
                        files[norm_key(rel)] = make_record(
                            rel=rel, path=entry.path, size=st.st_size, mtime=st.st_mtime
                        )
                        n_files += 1
                        n_bytes += st.st_size
                    except (PermissionError, OSError) as e:
                        errors.append({"path": entry.path, "error": str(e)})
        except (PermissionError, OSError) as e:
            errors.append({"path": cur, "error": str(e)})

        now = time.time()
        if on_progress and (now - last_push > 0.2 or not stack):
            last_push = now
            on_progress(dict(phase="scan", root=root, dirs=n_dirs,
                             files=n_files, bytes=n_bytes,
                             elapsed=now - t0, current=cur))

    stats = {"root": root, "dirs": n_dirs, "files": n_files, "bytes": n_bytes,
             "elapsed": time.time() - t0, "cancelled": cancelled}
    return {"files": files, "errors": errors, "stats": stats}


# --------------------------------------------------------------------------
# 2. 比对
# --------------------------------------------------------------------------

def md5_of(path, chunk=4 * 1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def merge_backup_pools(pool_list):
    """
    把多个备份盘的索引合并成「一个备份池」。

    典型场景：主库在 G 盘，H 盘和 I 盘共同承担备份（可能是历史原因分开放的）。
    这时只要某个相对路径在 H 或 I 任一盘中存在，就算已备份——
    逐盘单独对比会把「H 有 I 没有」的文件误判成缺失，导致缺口数字虚高。

    同一个路径在多个盘都有时，保留体积最大的那一份作为代表
   （大的更可能是完整文件），并记录它到底分布在哪些盘。
    """
    merged = {}
    for label, files in pool_list:
        for k, rec in files.items():
            cur = merged.get(k)
            if cur is None:
                merged[k] = dict(rec)
                merged[k]["in_drives"] = [label]
            else:
                cur["in_drives"].append(label)
                if rec["size"] > cur["size"]:
                    cur.update({"rel": rec["rel"], "path": rec["path"],
                                "size": rec["size"], "mtime": rec["mtime"]})
    return merged


def compare(src_files, dst_files, verify_hash=False, on_progress=None, cancel=None,
            detect_content=True):
    """
    src_files / dst_files 都是 {key: record}
    返回：
      missing   源有备份无 —— 需要复制（已剔除「备份盘别处有同内容」的项）
      size_diff 两边都有但大小不同 —— 内容疑似不一致
      hash_diff 大小相同但内容不同（仅 verify_hash=True 时才有意义）
      same      两边完全一致
      extra     备份盘多出来的（源盘已无，可能是你手动删过）
    """
    missing, size_diff, hash_diff, same = [], [], [], []
    keys = list(src_files.keys())
    total = len(keys)
    t0 = time.time()

    for i, k in enumerate(keys):
        if cancel is not None and cancel.is_set():
            break
        s = src_files[k]
        d = dst_files.get(k)
        if d is None:
            missing.append({
                "rel": s["rel"], "src_path": s["path"], "size": s["size"],
                "mtime": s["mtime"],
            })
        else:
            row = {"rel": s["rel"], "src_path": s["path"], "dst_path": d["path"],
                   "src_size": s["size"], "dst_size": d["size"],
                   "src_mtime": s["mtime"], "dst_mtime": d["mtime"]}
            if s["size"] != d["size"]:
                row["delta"] = s["size"] - d["size"]
                size_diff.append(row)
                continue
            if verify_hash:
                try:
                    hs, hd = md5_of(s["path"]), md5_of(d["path"])
                    if hs != hd:
                        row["src_md5"], row["dst_md5"] = hs, hd
                        hash_diff.append(row)
                        continue
                except OSError as e:
                    row["hash_error"] = str(e)
            same.append(row)
        if on_progress and i % 200 == 0:
            on_progress(dict(phase="compare", done=i + 1, total=total,
                             elapsed=time.time() - t0))

    extra = []
    for k, d in dst_files.items():
        if k not in src_files:
            extra.append({"rel": d["rel"], "dst_path": d["path"], "size": d["size"],
                          "mtime": d["mtime"]})

    result = {
        "missing": missing,
        "size_diff": size_diff,
        "hash_diff": hash_diff,
        "same": same,
        "extra": extra,
        "elapsed": time.time() - t0,
        "total_src": total,
    }
    if detect_content:
        cd, xcd = detect_content_dups(src_files, dst_files, missing, extra,
                                      on_progress=on_progress, cancel=cancel)
        result["missing"], result["extra"] = cd[0], cd[1]
        result["content_dup"] = xcd[0]
        result["extra_content_dup"] = xcd[1]
        result["intra_src_dup"] = xcd[2]
    else:
        result["content_dup"] = []
        result["extra_content_dup"] = []
        result["intra_src_dup"] = []
    return result


# 同大小文件数超过这个值就不做 MD5 细比对（大小都一样的文件通常是
# 空文件/占位文件，细比对性价比低；保守按「无同内容」处理，不会漏复制）。
_CONTENT_PEER_CAP = 200


def detect_content_dups(src_files, dst_files, missing, extra,
                        on_progress=None, cancel=None):
    """
    内容级查重（双向 + 源盘内部），解决「两个盘各自查重后互备又造出重复」的问题。

    背景：两个盘各自做过查重清理后，同一张照片可能只剩 G 盘一个路径、H 盘另一个路径
    （G 删了正本、H 删了副本）。按路径比对会互相认为「对方缺失」，互备就把同一张
    照片复制成两份；更糟的是源盘内部同一内容有多个路径时，会全搬过去，
    备份盘内部也出现重复。

    三层处理：
      1) content_dup        源有、备份无此路径，但备份别处有同内容 → 不复制
      2) extra_content_dup  备份有、源无此路径，但源别处有同内容 → 不补回
      3) intra_src_dup      源盘内部多个缺失项内容相同 → 只复制一个，其余不复制

    性能：只对「候选文件」和「对端同大小的文件」算 MD5
    （大小不同 MD5 必然不同，先用大小预筛），不做全盘哈希。

    返回 ((missing_keep, extra_keep), (content_dup, extra_content_dup, intra_src_dup))
    """
    def size_index(files):
        idx = {}
        for rec in files.values():
            idx.setdefault(rec["size"], []).append(rec)
        return idx

    def own_path(it):
        return it.get("src_path") or it.get("dst_path") or it.get("path")

    def split(cands, peer_files):
        idx = size_index(peer_files)
        keep, dups = [], []
        total = len(cands)
        for i, it in enumerate(cands):
            if cancel is not None and cancel.is_set():
                keep.extend(cands[i:])      # 取消时保守保留，不漏复制
                break
            peers = idx.get(it["size"]) or []
            if not peers or len(peers) > _CONTENT_PEER_CAP:
                keep.append(it)
            else:
                try:
                    h = md5_of(own_path(it))
                except OSError:
                    keep.append(it)         # 读不了按普通缺失处理
                    peers = None
                if peers:
                    hit = None
                    for p in peers:
                        try:
                            if md5_of(p["path"]) == h:
                                hit = p
                                break
                        except OSError:
                            continue
                    if hit:
                        dups.append({"rel": it["rel"], "path": own_path(it),
                                     "size": it["size"], "mtime": it.get("mtime", 0),
                                     "peer_path": hit["path"]})
                    else:
                        keep.append(it)
            if on_progress and (i % 50 == 0 or i == total - 1):
                on_progress(dict(phase="content", done=i + 1, total=total,
                                 elapsed=0.0))
        return keep, dups

    mk, cd = split(missing, dst_files)      # 缺失项 ↔ 备份盘找同内容
    ek, xcd = split(extra, src_files)       # 多余项 ↔ 源盘找同内容

    # ---- 源盘内部重复：缺失项里内容相同的，只复制一份 ----
    # 否则源盘有 P1/P2 两个路径的同一张照片、备份盘都没有时，
    # 会把两份都复制过去，备份盘内部产生重复（用户实测踩到的坑）。
    intra = []
    if mk:
        # 目标盘已有目录集合：优先保留「目录在备份盘已存在」的那个路径，结构更整齐
        dst_dirs = set()
        for rec in dst_files.values():
            d = os.path.dirname(rec["rel"])
            while d:
                dst_dirs.add(d.lower())
                d = os.path.dirname(d)
        by_size = {}
        for it in mk:
            by_size.setdefault(it["size"], []).append(it)
        drop_ids = set()
        for items in by_size.values():
            if len(items) < 2:
                continue                     # 大小没有第二份，不可能内容重复
            by_md5 = {}
            for it in items:
                try:
                    h = md5_of(own_path(it))
                except OSError:
                    continue
                if h:
                    by_md5.setdefault(h, []).append(it)
            for group in by_md5.values():
                if len(group) < 2:
                    continue
                group.sort(key=lambda it: (
                    os.path.dirname(it["rel"]).lower() not in dst_dirs,
                    len(it["rel"]), it["rel"]))
                for other in group[1:]:
                    intra.append({"rel": other["rel"], "path": own_path(other),
                                  "size": other["size"], "mtime": other.get("mtime", 0),
                                  "peer_path": own_path(group[0])})
                    drop_ids.add(id(other))
        if drop_ids:
            mk = [x for x in mk if id(x) not in drop_ids]
    return (mk, ek), (cd, xcd, intra)


def group_by_top_dir(rows, limit=60):
    """把缺失文件按「顶层子目录」归类，方便看分布"""
    buckets = {}
    for r in rows:
        rel = r.get("rel", "")
        top = rel.split("\\")[0] if "\\" in rel else "(根目录)"
        b = buckets.setdefault(top, {"name": top, "count": 0, "bytes": 0})
        b["count"] += 1
        b["bytes"] += r.get("size") or r.get("src_size") or 0
    out = sorted(buckets.values(), key=lambda x: -x["bytes"])
    return out[:limit]


def summarize(cmp_result, src_stats, dst_stats):
    m = cmp_result
    need_bytes = sum(r["size"] for r in m["missing"])
    return {
        "src_files": src_stats["files"],
        "src_bytes": src_stats["bytes"],
        "dst_files": dst_stats["files"],
        "dst_bytes": dst_stats["bytes"],
        "same": len(m["same"]),
        "same_bytes": sum(r["src_size"] for r in m["same"]),
        "missing": len(m["missing"]),
        "missing_bytes": need_bytes,
        "size_diff": len(m["size_diff"]),
        "hash_diff": len(m["hash_diff"]),
        "extra": len(m["extra"]),
        "content_dup": len(m.get("content_dup", [])),
        "content_dup_bytes": sum(r["size"] for r in m.get("content_dup", [])),
        "extra_content_dup": len(m.get("extra_content_dup", [])),
        "intra_src_dup": len(m.get("intra_src_dup", [])),
        "intra_src_dup_bytes": sum(r["size"] for r in m.get("intra_src_dup", [])),
        # 覆盖率 = 一致 / (一致+大小不一致+内容不同)。
        # 坑：原来写成 same/same + size_diff + hash_diff，运算符优先级导致
        # 先算 same/same，same=0 时直接除零崩溃（两盘路径大多不同时就会触发）。
        "coverage": (len(m["same"]) / (len(m["same"]) + len(m["size_diff"]) + len(m["hash_diff"])))
        if (len(m["same"]) + len(m["size_diff"]) + len(m["hash_diff"])) else 0.0,
        "scan_elapsed": (src_stats["elapsed"] + dst_stats["elapsed"]),
        "total_elapsed": src_stats["elapsed"] + dst_stats["elapsed"] + m["elapsed"],
    }


# --------------------------------------------------------------------------
# 3. 同步（只复制缺失项）
# --------------------------------------------------------------------------

def copy_missing(items, src_root, dst_root, dry_run=False, overwrite=False,
                 on_progress=None, cancel=None, verify_size=True):
    """
    items: compare() 得到的 missing 列表（或用户勾选的子集）
    目标路径 = dst_root + 相对路径  => 目录结构与文件名和源盘完全一致
    返回 {ok, skipped, failed, bytes_copied, log, elapsed}
    """
    dst_root = os.path.normpath(os.path.abspath(dst_root))
    t0 = time.time()
    ok = skipped = failed = 0
    nbytes = 0
    log = []
    total = len(items)

    for i, it in enumerate(items):
        if cancel is not None and cancel.is_set():
            log.append(("中断", "", "用户中止了复制"))
            break
        src_path = it.get("src_path") or os.path.join(src_root, it["rel"])
        rel = it["rel"]
        dst_path = os.path.normpath(os.path.join(dst_root, rel))
        dst_dir = os.path.dirname(dst_path)

        # 安全护栏：目标必须落在 dst_root 之内
        if not os.path.abspath(dst_path).startswith(os.path.abspath(dst_root)):
            failed += 1
            log.append(("失败", rel, "路径越界，已阻止"))
            continue

        if os.path.exists(dst_path):
            if not overwrite:
                skipped += 1
                continue
            if dry_run:
                ok += 1
                nbytes += it.get("size", 0)
                log.append(("将覆盖", rel, ""))
                continue

        if dry_run:
            ok += 1
            nbytes += it.get("size", 0)
            continue

        try:
            if dst_dir and not os.path.isdir(dst_dir):
                os.makedirs(dst_dir, exist_ok=True)
            # 复制到临时名再改名，避免中断留下半个文件
            tmp = dst_path + ".part_tmp"
            shutil.copy2(src_path, tmp)
            if verify_size:
                want, got = os.path.getsize(src_path), os.path.getsize(tmp)
                if want != got:
                    os.remove(tmp)
                    failed += 1
                    log.append(("失败", rel, f"大小核验不通过 {got}/{want}"))
                    continue
            if os.path.exists(dst_path):
                os.remove(dst_path)
            os.replace(tmp, dst_path)
            ok += 1
            nbytes += os.path.getsize(dst_path)
        except (PermissionError, OSError) as e:
            failed += 1
            log.append(("失败", rel, str(e)))

        if on_progress and i % 50 == 0:
            on_progress(dict(phase="copy", done=i + 1, total=total,
                             bytes=nbytes, elapsed=time.time() - t0,
                             current=rel))

    return {"ok": ok, "skipped": skipped, "failed": failed,
            "bytes_copied": nbytes, "log": log, "elapsed": time.time() - t0,
            "dry_run": dry_run, "dst_root": dst_root}


def free_space(path):
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def check_capacity(missing_bytes, dst_root):
    fs = free_space(dst_root)
    ok = fs["free"] - missing_bytes
    return {"free": fs["free"], "need": missing_bytes, "remain": ok,
            "enough": ok >= 0}


# --------------------------------------------------------------------------
# 4. 报告导出
# --------------------------------------------------------------------------

CSV_HEADER = ["相对路径", "源文件大小", "源修改时间", "备份文件大小",
              "备份修改时间", "状态", "说明"]


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return ""


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(r)


def build_report_rows(cmp_result, src_root, dst_root, dst_label=""):
    """把比对结果拍平成 CSV 行"""
    rows = []
    for r in cmp_result["missing"]:
        rows.append([r["rel"], r["size"], _fmt_time(r["mtime"]), "", "",
                     "缺失", "备份盘没有此文件，建议复制"])
    for r in cmp_result["size_diff"]:
        note = f"大小不一致，差 {human(abs(r['delta']))}" + ("（源更大）" if r["delta"] > 0 else "（备份更大）")
        rows.append([r["rel"], r["src_size"], _fmt_time(r["src_mtime"]),
                     r["dst_size"], _fmt_time(r["dst_mtime"]), "大小不一致", note])
    for r in cmp_result["hash_diff"]:
        rows.append([r["rel"], r["src_size"], _fmt_time(r["src_mtime"]),
                     r["dst_size"], _fmt_time(r["dst_mtime"]), "内容不同", "大小相同但 MD5 不一致"])
    for r in cmp_result["same"]:
        rows.append([r["rel"], r["src_size"], _fmt_time(r["src_mtime"]),
                     r["dst_size"], _fmt_time(r["dst_mtime"]), "一致", "已备份"])
    for r in cmp_result["extra"]:
        rows.append([r["rel"], "", "", r["size"], _fmt_time(r["mtime"]),
                     "备份盘多余", "源盘已不存在此文件"])
    for r in cmp_result.get("content_dup", []):
        rows.append([r["rel"], r["size"], _fmt_time(r["mtime"]), "", "",
                     "同内容异路径", "备份盘已有相同内容（%s），不复制以免重复" % r.get("peer_path", "")])
    for r in cmp_result.get("extra_content_dup", []):
        rows.append([r["rel"], "", "", r["size"], _fmt_time(r["mtime"]),
                     "同内容异路径", "源盘已有相同内容（%s），不补回以免重复" % r.get("peer_path", "")])
    for r in cmp_result.get("intra_src_dup", []):
        rows.append([r["rel"], r["size"], _fmt_time(r["mtime"]), "", "",
                     "源盘内部重复", "与 %s 内容相同，本次只复制一份，避免备份盘内部出现重复" % r.get("peer_path", "")])
    return rows


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>照片备份对比报告 · {dst_label}</title>
<style>
 :root{{color-scheme:dark}}
 body{{font-family:-apple-system,"Microsoft YaHei",Segoe UI,sans-serif;background:#0f1115;color:#e6e8eb;margin:0;padding:28px}}
 h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:28px 0 10px;color:#9fb3c8}}
 .meta{{color:#8b98a5;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px}}
 .card{{background:#171a21;border:1px solid #242932;border-radius:10px;padding:14px 18px;min-width:150px}}
 .card .k{{font-size:12px;color:#8b98a5}} .card .v{{font-size:21px;font-weight:600;margin-top:4px}}
 .v.red{{color:#ff6b6b}} .v.grn{{color:#3ddc84}} .v.yel{{color:#ffd166}} .v.blu{{color:#5aa9ff}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border-bottom:1px solid #242932;padding:7px 10px;text-align:left;vertical-align:top}}
 th{{background:#171a21;color:#9fb3c8;position:sticky;top:0}}
 td.path{{word-break:break-all;font-family:Consolas,monospace;font-size:12px}}
 .st-missing{{color:#ff6b6b;font-weight:600}} .st-same{{color:#3ddc84}}
 .st-size_diff,.st-hash_diff{{color:#ffd166;font-weight:600}} .st-extra{{color:#8b98a5}}
 .st-content_dup,.st-extra_content_dup{{color:#5aa9ff;font-weight:600}}
 .st-intra_src_dup{{color:#bc4c00;font-weight:600}}
 .scroll{{max-height:460px;overflow:auto;border:1px solid #242932;border-radius:8px}}
</style></head><body>
<h1>照片备份对比报告</h1>
<div class="meta">源目录 <code>{src_root}</code> &nbsp;→&nbsp; 备份目录 <code>{dst_root}</code>
 &nbsp;|&nbsp; 生成时间 {gen_time}</div>
<div class="cards">
 <div class="card"><div class="k">源盘文件</div><div class="v">{src_files}</div></div>
 <div class="card"><div class="k">备份盘文件</div><div class="v">{dst_files}</div></div>
 <div class="card"><div class="k">完全一致</div><div class="v grn">{same}</div></div>
 <div class="card"><div class="k">缺失待复制</div><div class="v red">{missing}</div></div>
 <div class="card"><div class="k">大小不一致</div><div class="v yel">{size_diff}</div></div>
 <div class="card"><div class="k">需复制体积</div><div class="v blu">{missing_h}</div></div>
 <div class="card"><div class="k">备份盘多余</div><div class="v">{extra}</div></div>
 <div class="card"><div class="k">同内容异路径</div><div class="v blu">{content_dup}</div></div>
 <div class="card"><div class="k">源盘内部重复</div><div class="v blu">{intra_src_dup}</div></div>
 <div class="card"><div class="k">耗时</div><div class="v">{elapsed}</div></div>
</div>
{body}
</body></html>"""


def build_html_report(cmp_result, summary, src_root, dst_root, dst_label="", max_rows=3000):
    def table(rows, cls_filter=None):
        if not rows:
            return ""
        out = ["<h2>{}</h2><div class='scroll'><table><tr><th>相对路径</th>"
               "<th>源大小</th><th>备份大小</th><th>状态</th><th>说明</th></tr>".format(
                   rows[0]["_title"])]
        for r in rows[1:]:
            if cls_filter and not cls_filter(r):
                continue
            sp = r.get("src_size", r.get("size", 0))
            dp = r.get("dst_size", "")
            st = r["_status"]
            note = r.get("_note", "")
            out.append(
                f"<tr><td class='path'>{r['rel']}</td><td>{human(sp)}</td>"
                f"<td>{human(dp) if dp != '' else '—'}</td>"
                f"<td class='st-{st}'>{r['_status']}</td><td>{note}</td></tr>")
        out.append("</table></div>")
        return "".join(out)

    def pack(rows, status, note_fn, title):
        out = [{"_title": title}]
        for r in rows:
            r2 = dict(r)
            r2["_status"] = status
            r2["_note"] = note_fn(r)
            out.append(r2)
        return out

    sections = []
    sections.append(table(pack(cmp_result["missing"], "missing",
                               lambda r: "备份盘没有此文件", "缺失文件（建议复制）")))
    sections.append(table(pack(cmp_result["size_diff"], "size_diff",
                               lambda r: "大小不一致，差 " + human(abs(r["delta"])),
                               "大小不一致（需要人工确认）")))
    sections.append(table(pack(cmp_result["hash_diff"], "hash_diff",
                               lambda r: "大小相同但内容不同", "内容不同（MD5 校验发现）")))
    sections.append(table(pack(cmp_result["extra"], "extra",
                               lambda r: "源盘已无此文件（不自动删除）", "备份盘多余文件")))
    sections.append(table(pack(cmp_result.get("content_dup", []), "content_dup",
                               lambda r: "备份盘已有相同内容：" + r.get("peer_path", ""),
                               "同内容异路径（不复制到备份盘，避免互备制造重复）")))
    sections.append(table(pack(cmp_result.get("extra_content_dup", []), "extra_content_dup",
                               lambda r: "源盘已有相同内容：" + r.get("peer_path", ""),
                               "同内容异路径（不补回源盘，避免互备制造重复）")))
    sections.append(table(pack(cmp_result.get("intra_src_dup", []), "intra_src_dup",
                               lambda r: "源盘内已有相同内容（" + r.get("peer_path", "") + "），只复制一份",
                               "源盘内部重复（同一内容多个路径，只复制一个到备份盘）")))
    sections.append(table(pack(cmp_result["same"], "same",
                               lambda r: "已备份", "完全一致（无需操作）")))

    return HTML_TMPL.format(
        src_root=src_root, dst_root=dst_root, dst_label=dst_label,
        gen_time=_fmt_time(time.time()),
        src_files=summary["src_files"], dst_files=summary["dst_files"],
        same=summary["same"], missing=summary["missing"],
        size_diff=summary["size_diff"], extra=summary["extra"],
        content_dup=summary.get("content_dup", 0),
        intra_src_dup=summary.get("intra_src_dup", 0),
        missing_h=human(summary["missing_bytes"]),
        elapsed=f"{summary['total_elapsed']:.1f} 秒",
        body="".join(sections))