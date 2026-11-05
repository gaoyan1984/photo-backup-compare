# -*- coding: utf-8 -*-
"""
照片备份对比 / 补齐工具 —— 本地服务 + 网页界面
启动：python app.py  （自动打开浏览器）
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import socket
import string
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "报告输出")
os.makedirs(OUT_DIR, exist_ok=True)

LOG_PATH = os.path.join(BASE_DIR, "服务日志.txt")


def write_log(msg):
    """把启动/运行情况写进日志文件。
    pythonw 是无窗口运行的，一出错用户什么都看不到，
    所以必须落盘，否则「装了自启却没反应」根本无法排查。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(msg)


def read_log_tail(n=40):
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "（还没有日志）\n"

PORT = int(os.environ.get("PBS_PORT", "8765"))
HOST = "127.0.0.1"

# ---------------------------------------------------------------- 任务管理
JOBS = {}
JOBS_LOCK = threading.Lock()


def new_job(kind="scan"):
    jid = f"{kind}_{int(time.time() * 1000)}"
    job = {"id": jid, "kind": kind, "state": "running", "stage": "初始化",
           "progress": 0.0, "detail": "", "created": time.time(),
           "result": None, "error": None, "cancel": threading.Event(),
           "logs": []}
    with JOBS_LOCK:
        JOBS[jid] = job
    return job


def get_job(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)


def set_job(job, **kw):
    with JOBS_LOCK:
        job.update(kw)


def push_log(job, text):
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {text}")
    if len(job["logs"]) > 3000:
        del job["logs"][:1000]


def list_drives():
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            u = shutil.disk_usage(root)
            out.append({"letter": letter, "root": root, "total": u.total,
                        "used": u.used, "free": u.free,
                        "free_h": engine.human(u.free),
                        "total_h": engine.human(u.total)})
        except OSError:
            pass
    return out


def guess_roles(drives):
    """智能推荐：最大且剩余空间多的盘作为源，最小的两个作为备份目标"""
    usable = [d for d in drives if d["free"] > 0]
    if not usable:
        return {"src": "", "dst": []}
    src = sorted(usable, key=lambda d: -d["total"])[0]
    rest = sorted([d for d in usable if d["letter"] != src["letter"]],
                  key=lambda d: d["free"])
    return {"src": src["letter"], "dst": [d["letter"] for d in rest[:2]]}


# ---------------------------------------------------------------- 扫描任务
def _safe_label(s):
    """把盘符/路径转成能当文件名的安全字符串（去掉 : \\ / 等非法字符）"""
    for ch in ':\\/*?"<>|':
        s = s.replace(ch, "_")
    return s.strip("._ ") or "backup"


def _finish_result(job, src, per_dst):
    """生成报告文件并收尾"""
    set_job(job, stage="生成报告", progress=0.96)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for pd in per_dst:
        cmp_res = pd.pop("_cmp")
        label = _safe_label(pd.get("label") or
                            os.path.basename(pd["dst_root"].rstrip("\\/")) or "backup")
        base = f"对比报告_{label}_{stamp}"
        csv_path = os.path.join(OUT_DIR, base + ".csv")
        html_path = os.path.join(OUT_DIR, base + ".html")
        try:
            engine.write_csv(csv_path, engine.build_report_rows(cmp_res, "", pd["dst_root"], label))
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(engine.build_html_report(cmp_res, pd["summary"],
                                                job["src_root_display"],
                                                pd["dst_root"], label))
            pd["reports"] = [{"csv": csv_path, "html": html_path}]
        except OSError as e:
            push_log(job, f"  ! 报告写入失败 {base}: {e}")
            pd["reports"] = []
        pd["_cmp"] = cmp_res     # 复制阶段还要用，重新挂回

    set_job(job, state="done", stage="完成", progress=1.0,
            result={"src_root": src["stats"]["root"],
                    "src_roots": job.get("src_roots", [src["stats"]["root"]]),
                    "src_stats": src["stats"],
                    "src_errors": src["errors"][:100],
                    "targets": per_dst,
                    "out_dir": OUT_DIR,
                    "pool": job.get("pool", False)})


def build_single_result(src, dst, dstscan, idx, n, verify_hash, job, solo=False):
    """逐盘比对：源盘 vs 单个备份盘"""
    set_job(job, stage=f"比对 {dst}", progress=0.75 + 0.2 * idx / n, detail=dst)

    def on_cmp(p, _i=idx):
        set_job(job, stage=f"比对 {_i + 1}/{n}",
                progress=0.75 + 0.2 * (_i + (p["done"] / max(p["total"], 1))) / n,
                detail=f"{p['done']} / {p['total']}")

    cmp_res = engine.compare(src["files"], dstscan["files"], verify_hash,
                             on_progress=on_cmp, cancel=job["cancel"])
    summ = engine.summarize(cmp_res, src["stats"], dstscan["stats"])
    if summ["content_dup"] or summ["extra_content_dup"] or summ["intra_src_dup"]:
        push_log(job, f"  ⓘ 内容级查重：{summ['content_dup']} 个备份盘已有不复制、"
                      f"{summ['extra_content_dup']} 个源盘已有不补回、"
                      f"{summ['intra_src_dup']} 个源盘内部重复只复制一份（避免互备/内部制造重复）")
    cap = engine.check_capacity(summ["missing_bytes"], dst)
    # 反向补齐：把备份盘多余的补回源盘时，源盘容量才是约束条件
    rcap = engine.check_capacity(sum(r["size"] for r in cmp_res["extra"]),
                                 src["stats"]["root"])
    push_log(job, f"比对完成 {dst}: 一致 {summ['same']}, 缺失 {summ['missing']} "
                  f"({engine.human(summ['missing_bytes'])}), 大小不一致 {summ['size_diff']}, "
                  f"多余 {summ['extra']}, 备份盘剩余 {engine.human(cap['free'])}")
    return {
        "dst_root": dst,
        "label": os.path.basename(dst.rstrip("\\/")) or dst,
        "summary": summ,
        "capacity": cap,
        "reverse_capacity": rcap,
        "groups": engine.group_by_top_dir(cmp_res["missing"]),
        "extra_groups": engine.group_by_top_dir(cmp_res["extra"]),
        "content_dup": cmp_res.get("content_dup", []),
        "extra_content_dup": cmp_res.get("extra_content_dup", []),
        "intra_src_dup": cmp_res.get("intra_src_dup", []),
        "content_dup_groups": engine.group_by_top_dir(cmp_res.get("content_dup", [])),
        "extra_content_dup_groups": engine.group_by_top_dir(cmp_res.get("extra_content_dup", [])),
        "intra_src_dup_groups": engine.group_by_top_dir(cmp_res.get("intra_src_dup", [])),
        "sample_content_dup": cmp_res.get("content_dup", [])[:50],
        "sample_extra_content_dup": cmp_res.get("extra_content_dup", [])[:50],
        "sample_intra_src_dup": cmp_res.get("intra_src_dup", [])[:50],
        "content_dup_bytes": sum(r["size"] for r in cmp_res.get("content_dup", [])),
        "sample_missing": cmp_res["missing"][:50],
        "sample_extra": cmp_res["extra"][:50],
        "extra_bytes": sum(r["size"] for r in cmp_res["extra"]),
        "errors": dstscan["errors"][:50],
        "pool_members": [dst],
        "_cmp": cmp_res,      # 仅内存，供复制/报告使用，不发给前端
    }


def build_pool_result(src, dst_scans, verify_hash, job):
    """
    合并备份池比对：把多个备份盘当成一个整体。
    「任一备份盘有该文件」即算已备份，只有所有盘都没有才算缺失。
    """
    set_job(job, stage="合并备份池比对", progress=0.75,
            detail=" + ".join(d for d, _ in dst_scans))

    pool_list = []
    for dst, ds in dst_scans:
        label = os.path.basename(dst.rstrip("\\/")) or dst
        pool_list.append((label, ds["files"]))
    merged = engine.merge_backup_pools(pool_list)

    def on_cmp(p):
        set_job(job, stage="合并备份池比对",
                progress=0.75 + 0.2 * (p["done"] / max(p["total"], 1)),
                detail=f"{p['done']} / {p['total']}")

    cmp_res = engine.compare(src["files"], merged, verify_hash,
                             on_progress=on_cmp, cancel=job["cancel"])

    # 汇总口径要按「池子」来算，不能拿单个盘的 stats
    pool_files = sum(ds["stats"]["files"] for _, ds in dst_scans)
    pool_bytes = sum(ds["stats"]["bytes"] for _, ds in dst_scans)
    summ = engine.summarize(cmp_res, src["stats"],
                            {"files": pool_files, "bytes": pool_bytes,
                             "elapsed": sum(ds["stats"]["elapsed"] for _, ds in dst_scans),
                             "dirs": 0})
    if summ["content_dup"] or summ["extra_content_dup"] or summ["intra_src_dup"]:
        push_log(job, f"  ⓘ 内容级查重：{summ['content_dup']} 个备份池已有不复制、"
                      f"{summ['extra_content_dup']} 个源盘已有不补回、"
                      f"{summ['intra_src_dup']} 个源盘内部重复只复制一份（避免互备/内部制造重复）")
    missing_bytes = summ["missing_bytes"]
    # 空间约束：看每个备份盘各自还能装多少（补文件时任选其一）
    caps = [{"dst_root": d, **engine.check_capacity(missing_bytes, d)}
            for d, _ in dst_scans]
    rcap = engine.check_capacity(sum(r["size"] for r in cmp_res["extra"]),
                                 src["stats"]["root"])
    names = " + ".join(os.path.basename(d.rstrip("\\/")) or d for d, _ in dst_scans)
    label = "合并池_" + "_".join(
        _safe_label(os.path.basename(d.rstrip("\\/")) or d) for d, _ in dst_scans)
    push_log(job, f"合并池比对完成（{names} 视为一个备份池）")
    push_log(job, f"  一致 {summ['same']}, 缺失 {summ['missing']} "
                  f"({engine.human(missing_bytes)}), 大小不一致 {summ['size_diff']}, "
                  f"多余 {summ['extra']}")
    for c in caps:
        push_log(job, f"  {c['dst_root']} 剩余 {engine.human(c['free'])}"
                      f"（{'够' if c['enough'] else '不够'}装 {engine.human(missing_bytes)}）")
    return {
        "dst_root": " + ".join(d for d, _ in dst_scans),
        "label": label,
        "summary": summ,
        "capacity": caps[0] if caps else {},
        "capacities": caps,
        "reverse_capacity": rcap,
        "groups": engine.group_by_top_dir(cmp_res["missing"]),
        "extra_groups": engine.group_by_top_dir(cmp_res["extra"]),
        "content_dup": cmp_res.get("content_dup", []),
        "extra_content_dup": cmp_res.get("extra_content_dup", []),
        "intra_src_dup": cmp_res.get("intra_src_dup", []),
        "content_dup_groups": engine.group_by_top_dir(cmp_res.get("content_dup", [])),
        "extra_content_dup_groups": engine.group_by_top_dir(cmp_res.get("extra_content_dup", [])),
        "intra_src_dup_groups": engine.group_by_top_dir(cmp_res.get("intra_src_dup", [])),
        "sample_content_dup": cmp_res.get("content_dup", [])[:50],
        "sample_extra_content_dup": cmp_res.get("extra_content_dup", [])[:50],
        "sample_intra_src_dup": cmp_res.get("intra_src_dup", [])[:50],
        "content_dup_bytes": sum(r["size"] for r in cmp_res.get("content_dup", [])),
        "sample_missing": cmp_res["missing"][:50],
        "sample_extra": cmp_res["extra"][:50],
        "extra_bytes": sum(r["size"] for r in cmp_res["extra"]),
        "errors": [],
        "pool_members": [d for d, _ in dst_scans],
        "_cmp": cmp_res,
    }


def run_scan(cfg, job):
    exts = cfg.get("exts")
    if exts == ["__ALL__"]:
        exts = None
    ignore_dirs = [x for x in (cfg.get("ignore_dirs") or []) if x.strip()]
    verify_hash = bool(cfg.get("verify_hash"))
    # pool=True：多个备份盘合并视为「一个备份池」。
    # 只要任一备份盘有某个文件，就算已备份；缺失 = 所有备份盘都没有。
    # 逐盘单独对比会把「A 盘有、B 盘没有」误判成缺失，导致缺口虚高。
    pool = bool(cfg.get("pool", False))
    src_roots = [os.path.normpath(s) for s in (cfg.get("src_roots") or []) if s]
    if not src_roots:   # 兜底：兼容旧调用
        src_roots = [os.path.normpath(cfg.get("src_root", ""))]
    dst_roots = cfg.get("dst_roots") or []
    job["pool"] = pool
    job["src_roots"] = src_roots

    set_job(job, stage="扫描源盘", progress=0.02, detail=" + ".join(src_roots))

    def on_src(p, _r):
        set_job(job, stage=f"扫描源盘 {_r}", progress=0.02 + 0.23 * min(1.0, p["elapsed"] / 45.0),
                detail=f"{p['files']} 个文件 / {engine.human(p['bytes'])}  @ {p['current']}")

    src_scans = []
    n_src = len(src_roots)
    for i, sr in enumerate(src_roots):
        if job["cancel"].is_set():
            set_job(job, state="cancelled", stage="已取消")
            return
        push_log(job, f"开始扫描源盘 {sr}（{i + 1}/{n_src}）")
        sc = engine.scan_tree(sr, exts, ignore_dirs,
                              on_progress=lambda p, _r=sr: on_src(p, _r),
                              cancel=job["cancel"])
        src_scans.append((sr, sc))
        push_log(job, f"源盘 {sr}: {sc['stats']['files']} 个文件, "
                      f"{engine.human(sc['stats']['bytes'])}, 耗时 {sc['stats']['elapsed']:.1f}s, "
                      f"不可访问 {len(sc['errors'])} 处")
        for e in sc["errors"][:20]:
            push_log(job, f"  ! {e['path']} -> {e['error']}")

    # 多个源盘合并成一份「主库并集」：任一源盘有该文件即算已有，
    # 重复处保留体积最大的一份作为代表（与合并备份池同口径）。
    if len(src_scans) == 1:
        src = src_scans[0][1]
    else:
        pool_list = [(os.path.basename(s.rstrip("\\/")) or s, sc["files"])
                     for s, sc in src_scans]
        merged = engine.merge_backup_pools(pool_list)
        src = {
            "files": merged,
            "errors": [e for _, sc in src_scans for e in sc["errors"]],
            "stats": {
                "root": src_roots[0],
                "dirs": 0,
                "files": len(merged),
                "bytes": sum(r["size"] for r in merged.values()),
                "elapsed": sum(sc["stats"]["elapsed"] for _, sc in src_scans),
                "cancelled": any(sc["stats"].get("cancelled") for _, sc in src_scans),
            },
        }
        push_log(job, f"已合并 {n_src} 个源盘为主库并集：去重后 {len(merged)} 个文件，"
                      f"共 {engine.human(src['stats']['bytes'])}")

    # 先扫描所有备份盘，再决定是合并比对还是逐盘比对
    dst_scans = []
    n = len(dst_roots) or 1
    for i, dst in enumerate(dst_roots):
        if job["cancel"].is_set():
            set_job(job, state="cancelled", stage="已取消")
            return
        dst = os.path.normpath(dst)

        def on_dst(p, _i=i, _d=dst):
            set_job(job, stage=f"扫描备份盘 {_d}",
                    progress=0.25 + 0.5 * (_i + min(1.0, p["elapsed"] / 60)) / n,
                    detail=f"{p['files']} 个文件 / {engine.human(p['bytes'])}")

        push_log(job, f"开始扫描备份盘 {dst}")
        ds = engine.scan_tree(dst, exts, ignore_dirs, on_progress=on_dst,
                              cancel=job["cancel"])
        if ds["stats"]["files"] == 0 and not os.path.isdir(dst):
            push_log(job, f"  ! 备份目录不存在，将按需创建: {dst}")
        dst_scans.append((dst, ds))

    per_dst = []
    if pool and len(dst_scans) > 1:
        per_dst.append(build_pool_result(src, dst_scans, verify_hash, job))
    else:
        if pool and len(dst_scans) == 1:
            push_log(job, "只有一个备份盘，合并池模式等同于逐盘比对")
        for i, (dst, ds) in enumerate(dst_scans):
            r = build_single_result(src, dst, ds, i, n, verify_hash,
                                    job, len(dst_scans) == 1)
            per_dst.append(r)
            summ, cap, rcap = r["summary"], r["capacity"], r["reverse_capacity"]
            push_log(job, f"比对完成 {dst}: 一致 {summ['same']}, 缺失 {summ['missing']} "
                          f"({engine.human(summ['missing_bytes'])}), 大小不一致 {summ['size_diff']}, "
                          f"多余 {summ['extra']} ({engine.human(rcap['need'])}), "
                          f"备份盘剩余 {engine.human(cap['free'])}")

    # 生成报告文件并收尾
    _finish_result(job, src, per_dst)


# ---------------------------------------------------------------- 复制任务
def run_copy(cfg, job):
    src_roots = [os.path.normpath(s) for s in (cfg.get("src_roots") or []) if s]
    if not src_roots:   # 兜底：兼容旧调用
        src_roots = [os.path.normpath(cfg.get("src_root", ""))]
    dst_root = os.path.normpath(cfg["dst_root"])
    rels = cfg.get("rels")
    dry_run = bool(cfg.get("dry_run", True))
    overwrite = bool(cfg.get("overwrite", False))
    verify_hash = bool(cfg.get("verify_hash"))
    # reverse=True 表示「把备份盘有、源盘没有的文件补回源盘」
    reverse = bool(cfg.get("reverse", False))
    exts = cfg.get("exts")
    if exts == ["__ALL__"]:
        exts = None
    verb_dir = "补回源盘" if reverse else "补到备份盘"
    src_display = " + ".join(src_roots)

    set_job(job, stage="准备", progress=0.01,
            detail=f"{src_display} → {dst_root}　（{verb_dir}）")

    def scan_prog(p):
        set_job(job, stage="复制前复核源盘", progress=0.05 + 0.15 * min(1, p["elapsed"] / 30),
                detail=f"{p['files']} 个文件")

    # 多源：逐个扫描后合并成「主库并集」（与扫描阶段同口径）。
    # 每条记录的 src_path 指向真实所在盘，copy_missing 据此逐盘取文件，
    # 因此"只在第二个源盘"的文件也能被补进备份盘。
    src_scans = []
    for i, sr in enumerate(src_roots):
        if job["cancel"].is_set():
            set_job(job, state="cancelled", stage="已取消")
            return
        sc = engine.scan_tree(sr, exts, cfg.get("ignore_dirs"),
                              on_progress=scan_prog, cancel=job["cancel"])
        src_scans.append((sr, sc))
    if len(src_scans) == 1:
        src_files = src_scans[0][1]["files"]
    else:
        src_files = engine.merge_backup_pools(
            [(os.path.basename(sr.rstrip("\\/")) or sr, sc["files"])
             for sr, sc in src_scans])

    dst = engine.scan_tree(dst_root, exts, cfg.get("ignore_dirs"),
                           on_progress=lambda p: None, cancel=job["cancel"])

    if rels is None:                      # 全量缺失项（方向由 src/dst 决定）
        cmp_res = engine.compare(src_files, dst["files"], False,
                                 cancel=job["cancel"])
        items = cmp_res["missing"]
        n_cd = (len(cmp_res.get("content_dup", []))
                + len(cmp_res.get("extra_content_dup", []))
                + len(cmp_res.get("intra_src_dup", [])))
        if n_cd:
            push_log(job, f"  ⓘ 内容级查重跳过 {n_cd} 个文件"
                          f"（{len(cmp_res.get('content_dup', []))} 个另一端已有、"
                          f"{len(cmp_res.get('extra_content_dup', []))} 个补回侧已有、"
                          f"{len(cmp_res.get('intra_src_dup', []))} 个源盘内部重复只复制一份）")
    else:                                # 用户勾选的相对路径
        keys = {engine.norm_key(r) for r in rels}
        items = []
        for k, rec in src_files.items():
            if k in keys:
                items.append({"rel": rec["rel"], "src_path": rec["path"],
                              "size": rec["size"], "mtime": rec["mtime"]})

    # 二次保险：跳过目标已存在的文件（除非允许覆盖）
    items = [it for it in items if overwrite or not os.path.exists(os.path.join(dst_root, it["rel"]))]
    need = sum(i["size"] for i in items)
    cap = engine.check_capacity(need, dst_root)
    set_job(job, stage="空间检查", progress=0.22,
            detail=f"待复制 {len(items)} 个 / {engine.human(need)}；目标盘剩余 {engine.human(cap['free'])}")
    push_log(job, f"方向：{src_display} → {dst_root}（{verb_dir}）")
    push_log(job, f"待复制 {len(items)} 个文件，共 {engine.human(need)}，"
                  f"目标剩余 {engine.human(cap['free'])}，{'空间充足' if cap['enough'] else '★ 空间不足'}")
    if not cap["enough"] and not dry_run:
        set_job(job, state="error", error=f"目标盘空间不足：需 {engine.human(need)}，仅剩 {engine.human(cap['free'])}")
        return

    last = [0.0]

    def copy_prog(p):
        rate = p["bytes"] / max(p["elapsed"], 0.001)
        pct = 0.25 + 0.7 * (p["done"] / max(p["total"], 1))
        if time.time() - last[0] > 0.3 or p["done"] >= p["total"]:
            last[0] = time.time()
            set_job(job, stage="复制中", progress=pct,
                    detail=f"{p['done']}/{p['total']}  {engine.human(p['bytes'])}  "
                           f"{engine.human(rate)}/s  {p['current']}")

    res = engine.copy_missing(items, src_roots[0], dst_root, dry_run=dry_run,
                              overwrite=overwrite, on_progress=copy_prog,
                              cancel=job["cancel"])
    for status, rel, msg in res["log"][:500]:
        push_log(job, f"  [{status}] {rel} {msg}")
    verb = "预演" if dry_run else "实际复制"
    push_log(job, f"{verb}结束（{verb_dir}）：成功 {res['ok']}，跳过 {res['skipped']}，失败 {res['failed']}，"
                  f"写入 {engine.human(res['bytes_copied'])}，耗时 {res['elapsed']:.1f}s")
    set_job(job, state="done", stage="完成", progress=1.0, result=res)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoBackupTool/1.0"

    def log_message(self, fmt, *a):      # 静音访问日志
        pass

    # -------- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _send_file(self, path, name=None, inline=False):
        try:
            if not os.path.isfile(path):
                return self._json({"error": "文件不存在"}, 404)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".csv"):
                ctype = "text/csv; charset=utf-8"
            elif path.endswith(".html"):
                ctype = "text/html; charset=utf-8"
            data = io.open(path, "rb").read()
            name = name or os.path.basename(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if not inline:
                # 仅「下载报告」时强制附件下载；首页/index 必须内联显示，
                # 否则浏览器会把网页当文件下载（用户每次访问都被下载成 index.html）。
                # HTTP 响应头只能放 ASCII（BaseHTTPRequestHandler 按 latin-1 编码头，
                # 中文文件名会直接抛 UnicodeEncodeError 把连接搞崩）。
                # 正确做法：filename= 只放 ASCII 兜底名，中文名走 RFC 5987 的 filename*。
                ascii_name = "report" + os.path.splitext(name)[1].lower()
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{_urlq(name)}")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            # 关键：一旦 send_response 之后出错，响应头已发出，
            # 再调 _json 会让协议错乱、客户端看到连接被直接关闭。
            # 所以这里只能记录日志，不能再写响应。
            write_log(f"下载失败 {path}: {type(e).__name__}: {e}")
            try:
                traceback.print_exc()
            except Exception:
                pass

    # -------- routes
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._send_file(os.path.join(BASE_DIR, "index.html"),
                                   inline=True)
        if p == "/api/drives":
            ds = list_drives()
            return self._json({"drives": ds, "guess": guess_roles(ds),
                               "exts": engine.DEFAULT_EXTS})
        if p == "/api/job":
            job = get_job(q.get("id", [""])[0])
            if not job:
                return self._json({"error": "任务不存在"}, 404)
            snap = {k: v for k, v in job.items() if k != "cancel"}
            if job["kind"] == "scan" and job.get("result"):
                r = dict(job["result"])
                r["targets"] = [{k: v for k, v in t.items() if k != "_cmp"}
                                for t in r["targets"]]
                snap["result"] = r
            return self._json(snap)
        if p == "/api/download":
            return self._send_file(q.get("path", [""])[0],
                                   os.path.basename(q.get("path", ["report"])[0]))
        if p == "/api/openout":
            # 用资源管理器打开报告输出文件夹
            try:
                os.startfile(OUT_DIR)
                return self._json({"ok": True})
            except OSError as e:
                return self._json({"error": str(e)}, 200)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()

        if p == "/api/scan":
            src_roots = [s for s in (body.get("src_roots") or []) if s]
            if not src_roots and body.get("src_root"):   # 兼容旧的单源接口
                src_roots = [body["src_root"]]
            dsts = [d for d in (body.get("dst_roots") or []) if d]
            if not src_roots or not dsts:
                return self._json({"error": "请至少选择一个源目录和一个备份目录"}, 400)
            body["src_roots"] = src_roots
            job = new_job("scan")
            job["src_root_display"] = " + ".join(src_roots)
            threading.Thread(target=_safe, args=(run_scan, body, job),
                             daemon=True).start()
            return self._json({"id": job["id"]})

        if p == "/api/copy":
            if not body.get("dst_root"):
                return self._json({"error": "缺少目标目录"}, 400)
            job = new_job("copy")
            threading.Thread(target=_safe, args=(run_copy, body, job),
                             daemon=True).start()
            return self._json({"id": job["id"]})

        if p == "/api/cancel":
            job = get_job(body.get("id", ""))
            if job:
                job["cancel"].set()
                return self._json({"ok": True})
            return self._json({"error": "任务不存在"}, 404)

        if p == "/api/folder":
            # 列出某个目录下的子目录，供界面做路径联想
            root = body.get("path") or "C:\\"
            try:
                items = []
                for e in os.scandir(root):
                    if e.is_dir() and not e.name.startswith("~$"):
                        items.append(e.name)
                items.sort(key=str.casefold)
                return self._json({"path": root, "dirs": items[:800]})
            except OSError as e:
                return self._json({"error": str(e)}, 200)

        return self._json({"error": "not found"}, 404)


def _urlq(s):
    from urllib.parse import quote
    return quote(s)


def _safe(fn, *a):
    job = a[-1]
    try:
        fn(*a[:-1], job)
    except Exception as e:
        traceback.print_exc()
        set_job(job, state="error", error=str(e),
                logs=job["logs"] + [f"错误: {e}"])


def _maybe_open_browser(url):
    """只有用户手动启动时才自动开浏览器；开机自启时保持静默"""
    args = " ".join(sys.argv[1:]).lower()
    exe = os.path.basename(sys.executable).lower()
    if "--startup" in args or "--no-browser" in args:
        write_log("静默模式启动，不自动打开浏览器")
        return False
    if exe.startswith("pythonw"):        # 无窗口运行，别弹浏览器
        return False
    try:
        webbrowser.open(url)
    except Exception as e:
        write_log(f"打开浏览器失败：{e}")
    return True


class SingletonHTTPServer(ThreadingHTTPServer):
    """
    Windows 的 SO_REUSEADDR 语义与 Unix 不同：它允许多个进程绑定同一个端口，
    结果就是重复启动会起两个实例互相抢请求（表现为进度卡死、任务莫名消失）。
    这里改用 SO_EXCLUSIVEADDRUSE 独占端口，配合启动前的主动探测，确保全局只有一个实例。
    """
    allow_reuse_address = 0          # 与 SO_EXCLUSIVEADDRUSE 互斥，必须关掉

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def port_in_use(host=HOST, port=PORT, timeout=0.8):
    """主动探测端口是否已有服务在跑"""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def probe_our_service(timeout=2.0):
    """
    确认某个端口上跑的确实是本工具的服务，而不是其他软件。
    只看"端口能连上"是不够的：被别的程序占了端口会让我们误判成
    "服务已在运行"，然后直接退出，用户就永远打不开页面。
    """
    for host in (HOST, "localhost"):
        try:
            import http.client
            conn = http.client.HTTPConnection(host, PORT, timeout=timeout)
            conn.request("GET", "/api/drives")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status == 200:
                obj = json.loads(body.decode("utf-8"))
                if isinstance(obj.get("drives"), list):
                    return True
        except Exception:
            continue
    return False


def make_server(host, port):
    """按指定地址族创建单例服务器"""
    cls = type("Srv_" + host.replace(":", "_"), (SingletonHTTPServer,), {})
    cls.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    srv = cls((host, port), Handler, bind_and_activate=False)
    srv.server_bind()
    srv.server_activate()
    return srv


def start_servers(port):
    """
    同时监听 IPv4 环回(127.0.0.1)和 IPv6 环回(::1)。
    Windows 上浏览器输 localhost 常常解析成 ::1，只监听 127.0.0.1 的话
    用户输入 http://localhost:8765/ 会连不上，页面打不开。
    两个都尽量起，至少成功一个即可。
    """
    servers, ok_hosts = [], []
    for host in ("127.0.0.1", "::1"):
        try:
            srv = make_server(host, port)
            servers.append(srv)
            ok_hosts.append(host)
        except OSError as e:
            write_log(f"  ! {host}:{port} 绑定失败：{e}")
    return servers, ok_hosts


def main():
    url = f"http://{HOST}:{PORT}/"
    write_log("=" * 46)
    write_log(f"启动中：{os.path.basename(sys.executable)}  参数={sys.argv[1:]}")
    write_log(f"工作目录：{BASE_DIR}")

    # 已有实例在跑：验证身份后直接打开界面，不重复启动
    if probe_our_service():
        write_log("检测到本工具服务已在运行，不重复启动")
        print("=" * 58)
        print(" 服务已在运行中，不再重复启动")
        print(f" 界面地址: {url}")
        print("=" * 58)
        _maybe_open_browser(url)
        return

    servers, hosts = start_servers(PORT)
    if not servers:
        write_log(f"✘ 端口 {PORT} 绑定失败（可能被其他软件占用）")
        print(f"✘ 启动失败：端口 {PORT} 无法绑定（可能被其他软件占用）")
        return
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    write_log(f"✔ 启动成功，监听：{', '.join(hosts)}，地址 {url}")
    print("=" * 58)
    print(" 照片备份对比 / 补齐工具已启动")
    print(f" 界面地址: {url}")
    if "::1" in hosts:
        print(f" 也可访问  : http://localhost:{PORT}/")
    if "127.0.0.1" not in hosts:
        print(" 注意：IPv4 环回未能监听，只能用 localhost 访问")
    print(f" 报告输出: {OUT_DIR}")
    print(f" 运行日志: {LOG_PATH}")
    print(" 关闭此窗口即退出服务")
    print("=" * 58)
    _maybe_open_browser(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        write_log("收到退出信号，服务停止")
        print("\n已退出")


if __name__ == "__main__":
    main()