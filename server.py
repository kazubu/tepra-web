#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TEPRA Web 印刷サーバ (tpsr3700-fork 用フロントエンド)

Python 標準ライブラリのみで動作する単一ファイルの Web アプリ。
ブラウザからテキストラベルのプレビュー/印刷、テンプレート保存、
差し込み印刷 (CSV 行ごと / 系列リストの対応付け・組み合わせ) ができる。
複数行ラベルでは行ごとのサイズ比率 (例: 7:3) も指定できる。

環境変数:
  TEPRA_DRIVER       ドライバ (tpsr3700) バイナリのパス
                     (既定: このファイルと同じディレクトリの ./tpsr3700)
  TEPRA_VID          USB Vendor ID (16進, 例 0d8a)。ドライバへ --vid で渡す
  TEPRA_PID          USB Product ID (16進)。SR3900P では lsusb で確認した値を指定
  TEPRA_MODEL        プリンタ機種 sr3900p (既定) または sr3700p。ドライバへ
                     --model で渡す。解像度 (360/270 dpi) と各テープ幅の
                     ドット数、ジョブ内の機種依存バイトが切り替わる
  TEPRA_PORT         待ち受けポート (既定 8090)
  TEPRA_HOST         待ち受けアドレス (既定 0.0.0.0)
  TEPRA_DRY_RUN      "1" のとき印刷の代わりに --no-print --dump で
                     ジョブファイルを生成するだけにする (実機なし確認用)
  TEPRA_FAKE_TAPE    開発用モック: テープ幅取得 (GET /api/tape) が実機を
                     問い合わせず、この mm 数を装着テープとして返す。
                     UI 確認専用で、印刷には影響しない (既定: 無効)
  TEPRA_DRY_TAPE_MM  DRY_RUN 時、テープ幅「自動」の代わりに使う幅 (既定 18)
  TEPRA_TEMPLATES    テンプレート保存先 JSON (既定: ./templates.json)
  TEPRA_HISTORY      印刷履歴の保存先 JSON (既定: ./history.json)
  TEPRA_FONTS_DIR    フォント置き場 (既定: ./fonts)
"""

import base64
import codecs
import csv
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DRIVER = os.environ.get("TEPRA_DRIVER", str(BASE_DIR / "tpsr3700"))
VID = os.environ.get("TEPRA_VID", "")
PID = os.environ.get("TEPRA_PID", "")
MODEL = (os.environ.get("TEPRA_MODEL", "") or "sr3900p").strip().lower()
MODEL_DPI = {"sr3700p": 270.0, "sr3900p": 360.0}
if MODEL not in MODEL_DPI:
    raise SystemExit(f"TEPRA_MODEL は sr3700p か sr3900p を指定してください: {MODEL}")
PORT = int(os.environ.get("TEPRA_PORT", "8090"))
HOST = os.environ.get("TEPRA_HOST", "0.0.0.0")
DRY_RUN = os.environ.get("TEPRA_DRY_RUN", "") == "1"
DRY_TAPE_MM = os.environ.get("TEPRA_DRY_TAPE_MM", "18")
FAKE_TAPE = os.environ.get("TEPRA_FAKE_TAPE", "").strip()
TEMPLATES_PATH = Path(os.environ.get("TEPRA_TEMPLATES", str(BASE_DIR / "templates.json")))
HISTORY_PATH = Path(os.environ.get("TEPRA_HISTORY", str(BASE_DIR / "history.json")))
HISTORY_MAX = 100
FONTS_DIR = Path(os.environ.get("TEPRA_FONTS_DIR", str(BASE_DIR / "fonts")))
CLI_TIMEOUT = 90

CLI_LOCK = threading.Lock()
HISTORY_LOCK = threading.Lock()
# プレースホルダ名は日本語なども許す (CSV の日本語ヘッダに対応するため、
# 空白と波括弧以外の文字列を名前として扱う)。
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^\s{}]+)\s*\}\}")


# ---------------------------------------------------------------------
# ドライバ CLI 呼び出し
# ---------------------------------------------------------------------

def build_driver_args(opts):
    """印刷/プレビュー共通の CLI 引数を組み立てる (テキストは除く)。"""
    args = [DRIVER, "--model", MODEL]
    tape = str(opts.get("tape", "auto"))
    notes = []
    if tape != "auto":
        args += ["--tape-mm", tape]
    elif DRY_RUN:
        args += ["--tape-mm", DRY_TAPE_MM]
        notes.append(f"DRY_RUN: テープ幅は {DRY_TAPE_MM}mm と仮定")
    font = opts.get("font", "")
    if font:
        args += ["--font", str(Path(FONTS_DIR) / font)]
    size = opts.get("size", "")
    if size not in ("", None):
        args += ["--size", str(size)]
    ratios = opts.get("ratios", "")
    if isinstance(ratios, (list, tuple)):
        ratios = ",".join(str(r) for r in ratios if str(r).strip() not in ("", "None"))
    if ratios not in ("", None):
        # 行ごとのサイズ比率。個数不一致はドライバ側がエラーにする。ラベル長
        # との併用は固定長 (折り返しなし、収まらなければエラー) になる。
        args += ["--line-ratios", str(ratios)]
    align = opts.get("align", "left")
    if align in ("left", "center", "right"):
        args += ["--align", align]
    if opts.get("frame"):
        args += ["--frame"]
        frame_width = opts.get("frame_width", "")
        if frame_width not in ("", None):
            args += ["--frame-width", str(frame_width)]
    length_mm = opts.get("length_mm", "")
    if length_mm not in ("", None):
        args += ["--max-tape-length-mm", str(length_mm)]
    for key, flag in (("left_margin_mm", "--left-margin-mm"), ("right_margin_mm", "--right-margin-mm")):
        value = str(opts.get(key, "") or "").strip()
        if value:
            # 送り方向の余白 (mm)。Web はプリセット (なし 1/0, 小 4/3, 中 6/5,
            # 大 8/7) かカスタム値を常に送る。空ならドライバ既定 (左 4 / 右 3)。
            args += [flag, value]
    cut = str(opts.get("cut", "full") or "full")
    if cut in ("full", "half", "none"):
        args += ["--cut", cut]
    if opts.get("cable"):
        # ケーブル表示ラベル: テープ上端から測った印刷可能エリア内だけに
        # 印字する (既定は 24mm = 7.5mm、36mm = 12.5mm)。
        args += ["--cable"]
        cable_mm = str(opts.get("cable_mm", "") or "").strip()
        if cable_mm:
            args += ["--cable-mm", cable_mm]
    copies = str(opts.get("copies", "") or "").strip()
    if copies and copies != "1":
        # 1 ジョブ内で同じラベルを複数枚印刷する (カット設定に従って切り分け)。
        args += ["--copies", copies]
    if VID:
        args += ["--vid", VID]
    if PID:
        args += ["--pid", PID]
    return args, notes


def log(message):
    """サーバを起動したターミナルに 1 行ログを出す (HTTP ログと同じ出力先)。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def run_driver(args, timeout=CLI_TIMEOUT):
    """ドライバを実行し (returncode, stdout, stderr) を返す。

    ドライバの標準エラー出力はここで捕捉してしまうので、そのままでは
    どこにも残らない。印刷の診断 (プリンタが動作中になったか、何秒で
    idle に戻ったか) に要るので、ターミナルにも出す。
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"driver timeout after {timeout}s: {' '.join(args[1:])}")
        raise
    took = time.monotonic() - t0
    log(f"driver rc={proc.returncode} in {took:.1f}s: {' '.join(args[1:])}")
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            log(f"  | {line}")
    return proc.returncode, proc.stdout, proc.stderr


# プリンタの物理解像度 (driver の Model::dpi と同じ値: SR3700P 270 / SR3900P 360)。
# ラベル長はジョブの行数 (送り方向ドット数) からこの値で換算する。
DOTS_PER_INCH = MODEL_DPI[MODEL]


def resolve_tape_mm(opts):
    """表示用のテープ幅 (mm) を決める。自動で実機未確定なら None。"""
    tape = str(opts.get("tape", "auto"))
    if tape != "auto":
        try:
            return int(tape)
        except ValueError:
            return None
    if DRY_RUN:
        try:
            return int(DRY_TAPE_MM)
        except ValueError:
            return None
    if FAKE_TAPE:
        try:
            return int(FAKE_TAPE)
        except ValueError:
            return None
    return None


def do_preview(opts, text):
    """CLI の --preview-out で SVG を生成して返す (印刷経路と同一描画)。

    応答には実寸情報も含める: SVG の width 属性 = 送り方向の行数
    (ドット数) なので、360 dpi で mm に換算してラベル長とする。
    """
    args, notes = build_driver_args(opts)
    with tempfile.TemporaryDirectory() as td:
        svg_path = Path(td) / "preview.svg"
        full = args + ["--preview-out", str(svg_path), text]
        with CLI_LOCK:
            rc, _out, err = run_driver(full)
        if rc != 0:
            return {"ok": False, "error": err.strip() or f"driver exited {rc}"}
        svg = svg_path.read_text(encoding="utf-8")
    # ドライバの警告 (未キャプチャ幅のフォールバック等) は画面にも出す。
    notes += [line.strip() for line in err.splitlines() if line.startswith("warning:")]
    feed_rows = None
    m = re.search(r'<svg[^>]*\bwidth="(\d+)"', svg)
    if m:
        feed_rows = int(m.group(1))
    length_mm = None
    if feed_rows is not None:
        length_mm = round(feed_rows * 25.4 / DOTS_PER_INCH, 1)
    return {
        "ok": True,
        "svg": svg,
        "notes": notes,
        "tape_mm": resolve_tape_mm(opts),
        "length_mm": length_mm,
        "feed_rows": feed_rows,
        "dpi": DOTS_PER_INCH,
    }


def do_print(opts, text):
    """CLI を --skip-preview で実行して 1 枚印刷する (DRY_RUN 時はダンプ)。"""
    return do_print_labels(opts, [text])


def do_feed(mm, cut):
    """空白テープを mm だけ送る (公式ドライバの「テープ送り」相当)。cut は full/none。"""
    try:
        mm = float(mm)
    except (TypeError, ValueError):
        return {"ok": False, "error": "送り長は数値で指定してください"}
    if not 1 <= mm <= 500:
        return {"ok": False, "error": "送り長は 1〜500mm で指定してください"}
    cut = cut if cut in ("full", "half", "none") else "none"
    args = [DRIVER, "--model", MODEL, "--feed-mm", str(mm), "--cut", cut, "--skip-preview"]
    if VID:
        args += ["--vid", VID]
    if PID:
        args += ["--pid", PID]
    with tempfile.TemporaryDirectory() as td:
        if DRY_RUN:
            args += ["--no-print", "--dump", str(Path(td) / "feed.prn")]
        else:
            args += ["--wait"]
        with CLI_LOCK:
            rc, _out, err = run_driver(args, timeout=CLI_TIMEOUT + 60)
    if rc != 0:
        return {"ok": False, "error": err.strip() or f"driver exited {rc}"}
    what = "送ってカットしました" if cut != "none" else "送りました"
    return {"ok": True, "message": f"テープを {mm:g}mm {what}" + ("（DRY_RUN）" if DRY_RUN else "")}


def do_print_labels(opts, labels, cancel=None):
    """複数ラベルを 1 ジョブとして印刷する (DRY_RUN 時はダンプ)。

    ドライバの --labels-file (ASCII RS 区切り) に渡す。ジョブ内では
    ラベルごとに L/T/ラスタのブロックが並び、カット設定 (ハーフカット等)
    がラベル間にも適用される。

    cancel (threading.Event) が渡された場合、CLI_LOCK を取った直後 (ドライバ
    起動前) にも確認し、セットされていれば送信せずに cancelled を返す。
    """
    args, notes = build_driver_args(opts)
    with tempfile.TemporaryDirectory() as td:
        if len(labels) == 1:
            target = [labels[0]]
        else:
            labels_path = Path(td) / "labels.txt"
            labels_path.write_text("\x1e".join(labels), encoding="utf-8")
            target = ["--labels-file", str(labels_path)]
        if DRY_RUN:
            dump_path = Path(td) / "job.prn"
            full = args + ["--skip-preview", "--no-print", "--dump", str(dump_path)] + target
            with CLI_LOCK:
                if cancel is not None and cancel.is_set():
                    return {"ok": False, "cancelled": True, "error": "送信前に取り消しました"}
                rc, _out, err = run_driver(full)
            if rc != 0:
                return {"ok": False, "error": err.strip() or f"driver exited {rc}"}
            size = dump_path.stat().st_size
            return {
                "ok": True,
                "message": f"DRY_RUN: ジョブを生成しました ({len(labels)} 件, {size} bytes)。実機には送信していません。",
                "notes": notes,
            }
        # --wait: 印刷が終わるまでドライバが状態をポーリングして戻る。
        # 次のジョブを印刷中に送らないため (CLI_LOCK と併用)。
        full = args + ["--skip-preview", "--wait"] + target
        with CLI_LOCK:
            if cancel is not None and cancel.is_set():
                return {"ok": False, "cancelled": True, "error": "送信前に取り消しました"}
            rc, _out, err = run_driver(full, timeout=CLI_TIMEOUT + 180 + 30 * len(labels))
        if rc != 0:
            return {"ok": False, "error": err.strip() or f"driver exited {rc}"}
        return {"ok": True, "message": "印刷しました。", "notes": notes, "detail": err.strip()}


# ドライバのテープ幅インデックス表 (docs/protocol.md 1.5)
TAPE_INDEX_BY_MM = {4: 0, 6: 1, 9: 2, 12: 3, 18: 4, 24: 5, 36: 6, 50: 7}
SUPPORTED_TAPE_MM = (4, 6, 9, 12, 18, 24, 36)


def do_detect_tape():
    """本体に装着されているテープ幅をドライバの --detect-tape で取得する。

    TEPRA_FAKE_TAPE が設定されている場合は実機を問い合わせず、
    開発用モックとしてその値を返す (mock: true を明示する)。
    実機がない/見つからない場合はドライバのエラーをそのまま返す。
    """
    if FAKE_TAPE:
        try:
            mm = int(FAKE_TAPE)
        except ValueError:
            return {"ok": False, "error": f"TEPRA_FAKE_TAPE の値が不正です: {FAKE_TAPE}"}
        return {
            "ok": True,
            "tape_mm": mm,
            "index": TAPE_INDEX_BY_MM.get(mm),
            "supported": mm in SUPPORTED_TAPE_MM,
            "mock": True,
        }
    args = [DRIVER, "--detect-tape"]
    if VID:
        args += ["--vid", VID]
    if PID:
        args += ["--pid", PID]
    with CLI_LOCK:
        rc, out, err = run_driver(args, timeout=20)
    if rc != 0:
        return {"ok": False, "error": err.strip() or f"driver exited {rc}"}
    try:
        result = json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": f"ドライバの出力を解釈できませんでした: {out.strip()}"}
    result["ok"] = True
    result["mock"] = False
    return result


# ---------------------------------------------------------------------
# テンプレート保存 (JSON ファイル)
# ---------------------------------------------------------------------

def load_templates():
    if TEMPLATES_PATH.exists():
        try:
            return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_templates(data):
    TEMPLATES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------
# 印刷履歴 (JSON ファイル, 直近 HISTORY_MAX 件)
# ---------------------------------------------------------------------

def load_history():
    if HISTORY_PATH.exists():
        try:
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def record_history(entry):
    """履歴に1件追加して保存する。追加されたエントリ (id/ts 付き) を返す。"""
    entry = dict(entry)
    entry["id"] = uuid.uuid4().hex[:12]
    entry["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with HISTORY_LOCK:
        hist = load_history()
        hist.insert(0, entry)
        del hist[HISTORY_MAX:]
        HISTORY_PATH.write_text(
            json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return entry


def clear_history():
    with HISTORY_LOCK:
        HISTORY_PATH.write_text("[]", encoding="utf-8")


def find_history(entry_id):
    for entry in load_history():
        if entry.get("id") == entry_id:
            return entry
    return None


# ---------------------------------------------------------------------
# CSV の文字コード判定/デコード
# ---------------------------------------------------------------------

# UI の選択肢と Python コーデックの対応。Shift_JIS は Windows 系の
# 拡張を含む cp932 で読む (Excel の CSV が前提のため)。
CSV_CODECS = {"utf-8": "utf-8", "shift_jis": "cp932", "euc-jp": "euc_jp"}


def decode_csv_bytes(raw, encoding="auto"):
    """CSV のバイト列をデコードし (text, 判定した文字コード名) を返す。

    自動判定は決定的な順序で行う: BOM → UTF-8 → Shift_JIS(cp932) →
    EUC-JP の順に試し、最初にエラーなく読めたものを採用する。
    どれでも読めない場合は cp932 の置換デコードにフォールバックする。
    encoding に "utf-8"/"shift_jis"/"euc-jp" を渡すと手動指定になる。
    """
    if encoding and encoding != "auto":
        codec = CSV_CODECS.get(encoding)
        if codec is None:
            raise ValueError(f"未対応の文字コードです: {encoding}")
        return raw.decode(codec), encoding
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16"), "utf-16"
    for codec, label in (("utf-8", "utf-8"), ("cp932", "shift_jis"), ("euc_jp", "euc-jp")):
        try:
            return raw.decode(codec), label
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace"), "shift_jis"


# ---------------------------------------------------------------------
# 差し込み連続印刷のバックグラウンドジョブ (進捗表示/キャンセル用)
# ---------------------------------------------------------------------

MERGE_JOBS = {}
MERGE_JOBS_LOCK = threading.Lock()
MERGE_JOBS_KEEP = 20


def merge_job_view(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _merge_worker(job, labels, opts):
    # 全ラベルを 1 ジョブで送る (ラベル間のカット設定を活かすため)。
    # 送信後はプリンタ側で一気に印刷されるので、途中キャンセルは
    # 送信前にしか効かない。
    state = "done"
    error = None
    try:
        if job["_cancel"].is_set():
            state = "cancelled"
        else:
            job["phase"] = f"{len(labels)} 件を 1 ジョブとして送信・印刷中"
            r = do_print_labels(opts, labels, cancel=job["_cancel"])
            job["detail"] = (r.get("detail") or "").strip() or None
            if r.get("ok"):
                job["done"] = len(labels)
            elif r.get("cancelled"):
                state = "cancelled"
            else:
                state = "error"
                error = r.get("error", "unknown error")
    except Exception as e:  # タイムアウト等でもジョブを「実行中」のまま残さない
        state = "error"
        error = f"{type(e).__name__}: {e}"
    if state == "error":
        job["failed_index"] = 0
        job["failed_label"] = labels[0] if labels else None
        job["error"] = error
    job["state"] = state
    job["phase"] = None
    job["current_index"] = None
    job["current_label"] = None
    record_history({
        "kind": "merge",
        "labels": labels,
        "opts": opts,
        "ok": state == "done",
        "state": state,
        "done": job["done"],
        "total": job["total"],
        "error": error,
        # ドライバの状態ログ (印刷されなかったときの切り分け用)
        "detail": job.get("detail"),
    })


def start_merge_job(labels, opts):
    """連続印刷をバックグラウンドで開始し、ジョブ dict を返す。"""
    job = {
        "id": uuid.uuid4().hex[:12],
        "state": "running",
        "done": 0,
        "total": len(labels),
        "current_index": None,
        "current_label": None,
        "failed_index": None,
        "failed_label": None,
        "error": None,
        "phase": None,
        "detail": None,
        "_cancel": threading.Event(),
    }
    with MERGE_JOBS_LOCK:
        # 終了済みの古いジョブを掃除する (保持は直近 MERGE_JOBS_KEEP 件)
        finished = [j for j in MERGE_JOBS.values() if j["state"] != "running"]
        if len(finished) > MERGE_JOBS_KEEP:
            for old in finished[:-MERGE_JOBS_KEEP]:
                MERGE_JOBS.pop(old["id"], None)
        MERGE_JOBS[job["id"]] = job
    t = threading.Thread(target=_merge_worker, args=(job, labels, opts), daemon=True)
    t.start()
    return job


def get_merge_job(job_id):
    with MERGE_JOBS_LOCK:
        return MERGE_JOBS.get(job_id)


# ---------------------------------------------------------------------
# 差し込み: ラベル文字列の生成
# ---------------------------------------------------------------------

def substitute(template_text, values):
    def repl(m):
        key = m.group(1)
        return values.get(key, m.group(0))
    return PLACEHOLDER_RE.sub(repl, template_text)


def labels_from_csv(template_text, csv_text):
    """CSV (1行目ヘッダ = プレースホルダ名) の各行を 1 枚にする。"""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        raise ValueError("CSV が空です")
    labels = []
    records = []
    for row in reader:
        clean = {k.strip(): (v if v is not None else "") for k, v in row.items() if k}
        records.append(clean)
        labels.append(substitute(template_text, clean))
    return labels, records


def labels_from_series(template_text, series, mode):
    """系列リストから組み合わせを作る。

    series: {"A": ["aa","bb","cc"], "B": ["1","2","3"]} のような辞書。
    mode: "zip" = インデックス同士を組にする (短い方に合わせる)
          "cross" = 全組み合わせ (直積)
    戻り値: (labels, combos, meta)
    """
    names = list(series.keys())
    lists = [series[n] for n in names]
    meta = {"mode": mode, "lengths": {n: len(series[n]) for n in names}}
    combos = []
    if mode == "zip":
        n = min((len(v) for v in lists), default=0)
        meta["used"] = n
        meta["truncated"] = any(len(v) != n for v in lists)
        for i in range(n):
            combos.append({name: series[name][i] for name in names})
    elif mode == "cross":
        total = 1
        for v in lists:
            total *= len(v)
        meta["used"] = total
        meta["truncated"] = False
        for prod in itertools.product(*lists):
            combos.append(dict(zip(names, prod)))
    else:
        raise ValueError(f"unknown mode: {mode}")
    labels = [substitute(template_text, c) for c in combos]
    # 2系列 cross のときはマトリクス表示用データも返す
    matrix = None
    if mode == "cross" and len(names) == 2:
        a_name, b_name = names
        cells = []
        idx = 0
        for _a in series[a_name]:
            row = []
            for _b in series[b_name]:
                row.append(labels[idx])
                idx += 1
            cells.append(row)
        matrix = {
            "row_name": a_name,
            "col_name": b_name,
            "rows": series[a_name],
            "cols": series[b_name],
            "cells": cells,
        }
    return labels, combos, meta, matrix


def list_fonts():
    fonts = [{"name": "（バンドル）BIZ UDPゴシック", "file": ""}]
    if FONTS_DIR.is_dir():
        for p in sorted(FONTS_DIR.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                fonts.append({"name": p.name, "file": p.name})
    return fonts


# ---------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TepraWeb/1.0"

    def log_message(self, fmt, *args):  # 静かにする
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/tape":
            try:
                self._send_json(do_detect_tape())
            except subprocess.TimeoutExpired:
                self._send_json({"ok": False, "error": "テープ幅の取得がタイムアウトしました"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        elif self.path == "/api/fonts":
            self._send_json({"fonts": list_fonts()})
        elif self.path == "/api/templates":
            self._send_json({"templates": load_templates()})
        elif self.path == "/api/templates/export":
            body = json.dumps(load_templates(), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition", 'attachment; filename="tepra-templates.json"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/history":
            self._send_json({"history": load_history()})
        elif self.path.startswith("/api/merge/status"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            job = get_merge_job((qs.get("job") or [""])[0])
            if job is None:
                self._send_json({"ok": False, "error": "ジョブが見つかりません"}, 404)
            else:
                view = merge_job_view(job)
                view["ok"] = True
                self._send_json(view)
        elif self.path == "/api/config":
            self._send_json({
                "dry_run": DRY_RUN,
                "driver": DRIVER,
                "model": MODEL,
                "dpi": DOTS_PER_INCH,
                "pid": PID or "(default 0103)",
                "fake_tape": FAKE_TAPE or None,
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            data = self._read_json()
        except Exception as e:
            self._send_json({"ok": False, "error": f"bad JSON: {e}"}, 400)
            return
        try:
            if self.path == "/api/preview":
                self._send_json(do_preview(data.get("opts", {}), data.get("text", "")))
            elif self.path == "/api/feed":
                self._send_json(do_feed(data.get("mm", 10), data.get("cut", "none")))
            elif self.path == "/api/print":
                opts = data.get("opts", {})
                text = data.get("text", "")
                r = do_print(opts, text)
                record_history({
                    "kind": "single",
                    "text": text,
                    "opts": opts,
                    "ok": bool(r.get("ok")),
                    "error": r.get("error"),
                })
                self._send_json(r)
            elif self.path == "/api/templates":
                templates = load_templates()
                templates[data["name"]] = data["template"]
                save_templates(templates)
                self._send_json({"ok": True})
            elif self.path == "/api/templates/delete":
                templates = load_templates()
                templates.pop(data["name"], None)
                save_templates(templates)
                self._send_json({"ok": True})
            elif self.path == "/api/templates/import":
                self._handle_templates_import(data)
            elif self.path == "/api/decode-csv":
                raw = base64.b64decode(data.get("data_b64", ""))
                text, detected = decode_csv_bytes(raw, data.get("encoding", "auto"))
                self._send_json({"ok": True, "text": text, "encoding": detected})
            elif self.path == "/api/history/clear":
                clear_history()
                self._send_json({"ok": True})
            elif self.path == "/api/history/reprint":
                self._handle_history_reprint(data)
            elif self.path == "/api/labels":
                self._handle_labels(data)
            elif self.path == "/api/merge/preview":
                labels = data.get("labels", [])
                index = int(data.get("index", 0))
                if not (0 <= index < len(labels)):
                    self._send_json({"ok": False, "error": "index out of range"})
                    return
                self._send_json(do_preview(data.get("opts", {}), labels[index]))
            elif self.path == "/api/merge/print":
                # バックグラウンドジョブとして開始し、即座に job_id を返す。
                # 進捗は /api/merge/status、停止は /api/merge/cancel で扱う。
                labels = data.get("labels", [])
                if not labels:
                    self._send_json({"ok": False, "error": "ラベルがありません"})
                    return
                job = start_merge_job(labels, data.get("opts", {}))
                self._send_json({"ok": True, "job_id": job["id"], "total": job["total"]})
            elif self.path == "/api/merge/cancel":
                job = get_merge_job(data.get("job_id", ""))
                if job is None:
                    self._send_json({"ok": False, "error": "ジョブが見つかりません"}, 404)
                else:
                    job["_cancel"].set()
                    self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, 404)
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "ドライバがタイムアウトしました"})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _handle_templates_import(self, data):
        """テンプレート一式を取り込む。

        strategy: "overwrite" = 同名は上書き、"rename" = 同名は
        「名前 (2)」「名前 (3)」… と別名で追加する。
        """
        incoming = data.get("templates", {})
        if not isinstance(incoming, dict):
            self._send_json({"ok": False, "error": "templates はオブジェクトで指定してください"})
            return
        strategy = data.get("strategy", "rename")
        templates = load_templates()
        imported = []
        renamed = {}
        for name, tpl in incoming.items():
            target = name
            if target in templates:
                if strategy == "overwrite":
                    pass
                else:  # rename
                    n = 2
                    while f"{name} ({n})" in templates:
                        n += 1
                    target = f"{name} ({n})"
                    renamed[name] = target
            templates[target] = tpl
            imported.append(target)
        save_templates(templates)
        self._send_json({"ok": True, "imported": imported, "renamed": renamed})

    def _handle_history_reprint(self, data):
        entry = find_history(data.get("id", ""))
        if entry is None:
            self._send_json({"ok": False, "error": "履歴が見つかりません"}, 404)
            return
        opts = entry.get("opts", {})
        if entry.get("kind") == "merge":
            labels = entry.get("labels", [])
            if not labels:
                self._send_json({"ok": False, "error": "再印刷できるラベルがありません"})
                return
            job = start_merge_job(labels, opts)
            self._send_json({"ok": True, "async": True, "job_id": job["id"], "total": job["total"]})
        else:
            text = entry.get("text", "")
            r = do_print(opts, text)
            record_history({
                "kind": "single",
                "text": text,
                "opts": opts,
                "ok": bool(r.get("ok")),
                "error": r.get("error"),
                "reprint_of": entry.get("id"),
            })
            self._send_json(r)

    def _handle_labels(self, data):
        template_text = data.get("template", "")
        source = data.get("source", "csv")
        if source == "csv":
            csv_text = data.get("csv", "")
            detected = None
            if data.get("csv_b64"):
                # バイト列で受け取った場合はサーバ側でデコードする
                raw = base64.b64decode(data["csv_b64"])
                csv_text, detected = decode_csv_bytes(raw, data.get("encoding", "auto"))
            labels, records = labels_from_csv(template_text, csv_text)
            self._send_json({
                "ok": True,
                "labels": labels,
                "records": records,
                "meta": {"mode": "csv", "used": len(labels)},
                "matrix": None,
                "encoding": detected,
            })
        else:
            series = data.get("series", {})
            # 空行のみの系列は落とす
            series = {k: [v for v in vals] for k, vals in series.items()}
            labels, combos, meta, matrix = labels_from_series(
                template_text, series, data.get("mode", "zip")
            )
            self._send_json({
                "ok": True,
                "labels": labels,
                "records": combos,
                "meta": meta,
                "matrix": matrix,
            })




INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>TEPRA Web 印刷</title>
<style>
  :root {
    /* Tier 1: raw light/dark color pairs */
    --bg-l: #f4f5f7;      --bg-d: #131519;
    --surface-l: #ffffff; --surface-d: #1d2026;
    --text-l: #1b1d21;    --text-d: #e8eaed;
    --muted-l: #5f6368;   --muted-d: #a3aab3;
    --border-l: #767676;  --border-d: #6b7178;
    --accent-l: #0b57d0;  --accent-d: #8ab4f8;
    --on-accent-l: #ffffff; --on-accent-d: #131519;
    --ok-l: #146c1e;      --ok-d: #7bc47f;
    --err-l: #b3261e;     --err-d: #f28b82;
    --on-err-l: #ffffff;  --on-err-d: #331512;
    --hover-l: #eceff4;   --hover-d: #2a2e36;
    --track-l: #e3e5e8;   --track-d: #26292f;
    --thumb-l: #6f747a;   --thumb-d: #8a8f96;
    /* Tier 2: semantic tokens */
    --bg: light-dark(var(--bg-l), var(--bg-d));
    --surface: light-dark(var(--surface-l), var(--surface-d));
    --text: light-dark(var(--text-l), var(--text-d));
    --muted: light-dark(var(--muted-l), var(--muted-d));
    --border: light-dark(var(--border-l), var(--border-d));
    --accent: light-dark(var(--accent-l), var(--accent-d));
    --on-accent: light-dark(var(--on-accent-l), var(--on-accent-d));
    --ok: light-dark(var(--ok-l), var(--ok-d));
    --err: light-dark(var(--err-l), var(--err-d));
    --on-err: light-dark(var(--on-err-l), var(--on-err-d));
    --hover: light-dark(var(--hover-l), var(--hover-d));
    --scroll-track: light-dark(var(--track-l), var(--track-d));
    --scroll-thumb: light-dark(var(--thumb-l), var(--thumb-d));
    color-scheme: light dark;
    accent-color: var(--accent);
    scrollbar-color: var(--scroll-thumb) var(--scroll-track);
  }
  /* Fallback for browsers without light-dark(): switch tokens by media query */
  @supports not (color: light-dark(white, black)) {
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: var(--bg-d); --surface: var(--surface-d); --text: var(--text-d);
        --muted: var(--muted-d); --border: var(--border-d); --accent: var(--accent-d);
        --on-accent: var(--on-accent-d); --ok: var(--ok-d); --err: var(--err-d);
        --on-err: var(--on-err-d); --hover: var(--hover-d);
        --scroll-track: var(--track-d); --scroll-thumb: var(--thumb-d);
      }
    }
  }
  body { font-family: system-ui, sans-serif; margin: 0 auto; max-width: 900px; padding: 0 1em 2em; background: var(--bg); color: var(--text); line-height: 1.5; }
  h1 { font-size: clamp(1.3rem, 1.15rem + 1vw, 1.8rem); }
  h2 { font-size: 1.1em; margin-top: 0; border-bottom: 1px solid var(--border); padding-bottom: .2em; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1em 1.25em 1.25em; margin-block: 1em; }
  textarea, input, select, button { font: inherit; font-size: max(1rem, 1em); }
  .visually-hidden:where(:not(:focus-within, :active)) {
    position: absolute !important; clip-path: inset(50%) !important; overflow: hidden !important;
    width: 1px !important; height: 1px !important; margin: -1px !important; padding: 0 !important;
    border: 0 !important; white-space: nowrap !important;
  }
  textarea, input, select { background: var(--surface); color: inherit; border: 1px solid var(--border); border-radius: 6px; padding: .35em .5em; box-sizing: border-box; }
  textarea { width: 100%; box-sizing: border-box; }
  fieldset { border: 1px solid var(--border); border-radius: 8px; margin: 0 0 .5em; padding: .25em .75em .5em; min-width: 0; }
  legend { padding: 0 .3em; font-size: .95em; }
  .row { margin: .5em 0; display: flex; gap: .5em; flex-wrap: wrap; align-items: center; }
  .field { display: flex; flex-direction: column; gap: .25rem; flex: 1 1 9em; margin-block: .25em; }
  .field[hidden] { display: none; }
  .field > label { font-size: .9em; }
  /* ボタンと高さを揃える (欄名が上にあるぶん、行の中で下端を合わせる) */
  .field > :where(input:not([type="checkbox"]):not([type="radio"]), select) { min-height: 2.4em; }
  .row:has(> .field) { align-items: flex-end; }
  .row:has(> .field) > :not(.field) { margin-block: .25em; }
  button { padding: .45em 1em; cursor: pointer; background: var(--surface); color: inherit; border: 1px solid var(--border); border-radius: 8px; min-height: 2.4em; box-sizing: border-box; }
  button:hover:not(:disabled) { background: var(--hover); }
  button:disabled { opacity: .55; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.1); }
  button.danger { background: var(--err); border-color: var(--err); color: var(--on-err); font-weight: 600; }
  /* 初期設定に戻す: 他の操作と見分けがつくよう、控えめな色の輪郭ボタンにして各セクションの末尾右寄せに置く */
  .section-actions { display: flex; justify-content: space-between; align-items: center; gap: .5em; flex-wrap: wrap; margin-top: 1em; padding-top: .6em; border-top: 1px dashed var(--border); }
  .section-actions > .group { display: flex; align-items: center; gap: .5em; flex-wrap: wrap; }
  button.reset { background: transparent; color: var(--muted); border-color: var(--muted); }
  button.reset:hover:not(:disabled) { background: var(--hover); color: var(--text); }
  :where(button, input, select, textarea, a:any-link):focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 2px;
  }
  @media (pointer: coarse) {
    button, select, input:not([type="radio"]):not([type="checkbox"]):not([type="file"]) { min-height: 48px; }
  }
  #preview { border: 1px solid var(--border); border-radius: 8px; padding: .5em; overflow-x: auto; background: #fff; }
  #preview:empty { display: none; }
  #preview svg { max-width: 100%; height: auto; }
  .msg { white-space: pre-wrap; margin: .5em 0; }
  .msg:empty { margin: 0; }
  .err { color: var(--err); } .ok { color: var(--ok); }
  table.matrix { border-collapse: collapse; }
  table.matrix td, table.matrix th { border: 1px solid var(--border); padding: .2em .5em; }
  .labels-list { max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; padding: .3em; display: flex; flex-direction: column; gap: 2px; }
  /* flex: none がないと、件数が多いときに項目が縮んでスクロールせず潰れる */
  .labels-list button { flex: none; text-align: left; border-color: transparent; min-height: 0; padding: .15em .4em; white-space: pre-wrap; word-break: break-all; }
  .note { color: var(--muted); font-size: .9em; }
  .button { display: inline-flex; align-items: center; padding: .45em 1em; min-height: 2.4em; box-sizing: border-box; background: var(--surface); color: inherit; border: 1px solid var(--border); border-radius: 8px; text-decoration: none; cursor: pointer; }
  .button:hover { background: var(--hover); }
  /* ファイル選択: 見た目はボタン。キーボードでも押せるよう input は hidden にしない */
  .file-input { position: absolute; width: 1px; height: 1px; opacity: 0; }
  .file-input:focus-visible + .button { outline: 3px solid var(--accent); outline-offset: 2px; }
  progress { width: min(24em, 100%); height: 1.1em; accent-color: var(--accent); }
  #ruler { position: relative; height: 1.3em; margin-bottom: .15em; color: var(--muted); font-size: 10px; }
  #ruler .tick { position: absolute; bottom: 0; width: 1px; height: 5px; background: var(--muted); }
  #ruler .tick.major { height: 8px; }
  #ruler .tick-label { position: absolute; bottom: 8px; transform: translateX(-50%); white-space: nowrap; }
  .history-item { display: flex; gap: .6em; align-items: baseline; flex-wrap: wrap; padding: .45em .2em; border-bottom: 1px solid var(--border); }
  .history-item:last-child { border-bottom: none; }
  .history-item .h-time { color: var(--muted); font-size: .85em; white-space: nowrap; }
  .history-item .h-kind { font-size: .8em; border: 1px solid var(--border); border-radius: 999px; padding: 0 .55em; white-space: nowrap; }
  .history-item .h-text { flex: 1 1 12em; white-space: pre-wrap; word-break: break-all; }
  .history-item .h-status { word-break: break-all; }
  dialog { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 12px; padding: 1.25em; max-width: min(26em, 90vw); }
  dialog h2 { border-bottom: none; }
  dialog:modal { overscroll-behavior: contain; }
  dialog::backdrop { background: rgb(0 0 0 / .45); overflow: hidden; overscroll-behavior: contain; }
</style>
</head>
<body>
<main>
<h1>TEPRA Web 印刷</h1>
<div id="config" class="note"></div>

<section class="card" id="preview-section">
<h2 id="h-preview">プレビュー</h2>
<p id="preview-empty" class="note">「プレビュー」を押すか、差し込み印刷のラベルを選んでください。</p>
<div id="preview-size" class="note" role="status"></div>
<div id="preview"></div>
<p class="note" id="ruler-note" style="display:none">枠はテープの実寸、斜線は印字されない部分、目盛りは mm の目安です。</p>
</section>

<section class="card">
<h2 id="h-label">ラベル</h2>
<div class="row">
  <div class="field">
    <label for="tape">テープ幅</label>
    <select id="tape">
      <option value="auto">自動</option>
      <option value="4">4mm</option>
      <option value="6">6mm</option>
      <option value="9">9mm</option>
      <option value="12">12mm</option>
      <option value="18">18mm</option>
      <option value="24">24mm</option>
      <option value="36">36mm</option>
    </select>
  </div>
  <div class="field">
    <label for="font">フォント</label>
    <select id="font"></select>
  </div>
  <div class="field">
    <label for="size">サイズ(pt)</label>
    <input id="size" type="number" min="1" max="200" step="0.5" inputmode="decimal" placeholder="自動">
  </div>
  <div class="field">
    <label for="align">配置</label>
    <select id="align" aria-describedby="align-hint">
      <option value="left">左</option>
      <option value="center">中央</option>
      <option value="right">右</option>
    </select>
    <span id="align-hint" class="visually-hidden">複数行、またはラベル長を指定したときに各行を揃えます</span>
  </div>
  <div class="field">
    <label for="length_mm">ラベル長(mm)</label>
    <input id="length_mm" type="number" min="10" max="500" inputmode="decimal" placeholder="指定なし">
  </div>
  <div class="field">
    <label for="margin_preset">余白</label>
    <select id="margin_preset">
      <option value="none">なし（左1 / 右0）</option>
      <option value="xsmall">極小（左2 / 右1）</option>
      <option value="small" selected>小（左4 / 右3）</option>
      <option value="medium">中（左6 / 右5）</option>
      <option value="large">大（左8 / 右7）</option>
      <option value="custom">カスタム</option>
    </select>
  </div>
  <div class="field">
    <label for="left_margin_mm">左余白(mm)</label>
    <input id="left_margin_mm" type="number" min="0" max="100" step="0.1" inputmode="decimal" value="4" disabled>
  </div>
  <div class="field">
    <label for="right_margin_mm">右余白(mm)</label>
    <input id="right_margin_mm" type="number" min="0" max="100" step="0.1" inputmode="decimal" value="3" disabled>
  </div>
  <div class="field">
    <label for="frame_width">枠線の太さ(ドット)</label>
    <input id="frame_width" type="number" min="1" max="3" step="1" inputmode="numeric" value="2">
  </div>
  <div class="field">
    <label for="cut">カット</label>
    <select id="cut">
      <option value="full">フルカット</option>
      <option value="half">ハーフカット</option>
      <option value="none">カットしない</option>
    </select>
  </div>
  <div class="field">
    <label for="copies">枚数</label>
    <input id="copies" type="number" min="1" max="99" step="1" inputmode="numeric" value="1" aria-describedby="copies-note">
  </div>
</div>
<p class="note" id="copies-note">同じラベルを連続で印刷します。差し込み印刷では各ラベルに適用されます。</p>
<div class="row">
  <label><input type="checkbox" id="frame"> 枠線を付ける</label>
</div>
<div class="row">
  <label><input type="checkbox" id="cable"> ケーブル表示ラベル</label>
  <div class="field" style="flex: 0 1 11em">
    <label for="cable_mm">印刷エリア(mm)</label>
    <input id="cable_mm" type="number" min="1" max="50" step="0.5" inputmode="decimal" placeholder="既定" disabled>
  </div>
  <span class="note">テープ上端の印刷エリアだけに印字します。既定は 24mm が 7.5mm、36mm が 12.5mm。はみ出すときは小さくしてください。</span>
</div>
<div class="row">
  <button id="btn-tape-detect" type="button" aria-describedby="tape-note">装着テープを確認</button>
  <span id="tape-status" role="status">装着テープ: 未確認</span>
</div>
<div class="row">
  <div class="field" style="flex: 0 1 8em">
    <label for="feed_mm">送り長(mm)</label>
    <input id="feed_mm" type="number" min="1" max="500" step="1" inputmode="numeric" value="10">
  </div>
  <button id="btn-feed" type="button">テープを送る</button>
  <button id="btn-feed-cut" type="button">送ってカット</button>
  <span id="feed-status" role="status"></span>
</div>
<p class="note" id="tape-note">テープ幅「自動」ではプレビュー/印刷のたびに本体から幅を取得します。</p>
<div class="field">
  <label for="text">印刷テキスト</label>
  <textarea id="text" rows="3" aria-describedby="text-hint" placeholder="例: HOST-A&#10;10.0.0.1"></textarea>
  <span id="text-hint" class="note">改行で複数行になります。差し込みは {{A}} のように書きます。</span>
</div>
<div class="row" role="group" aria-labelledby="ratios-label">
  <span id="ratios-label">行ごとのサイズ比率:</span>
  <span id="ratios-inputs"></span>
</div>
<p class="note" id="ratios-note">行ごとの文字サイズの比率（例: 2行で 7 : 3）。空欄は 1。ラベル長と併用すると折り返さず、収まらなければエラーになります。</p>
<div class="section-actions">
  <div class="group">
    <button id="btn-preview" type="button">プレビュー</button>
    <button id="btn-print" type="button" class="primary">印刷</button>
  </div>
  <div class="group">
    <button id="btn-reset-label" type="button" class="reset">ラベルを初期設定に戻す</button>
  </div>
</div>
<div id="msg" class="msg" role="status"></div>
</section>

<section class="card">
<h2 id="h-tpl">テンプレート</h2>
<div class="row">
  <div class="field" style="flex: 0 1 14em">
    <label for="tpl-name">テンプレート名</label>
    <input id="tpl-name" placeholder="例: 機器ラベル">
  </div>
  <button id="btn-tpl-save" type="button">現在の設定を保存</button>
  <div class="field" style="flex: 0 1 12em">
    <label for="tpl-list">保存済みテンプレート</label>
    <select id="tpl-list"></select>
  </div>
  <button id="btn-tpl-load" type="button">読み込む</button>
  <button id="btn-tpl-delete" type="button">削除</button>
</div>
<p class="note">テキストも一緒に保存します。</p>
<div class="row">
  <a class="button" id="btn-tpl-export" href="/api/templates/export" download="tepra-templates.json">ファイルに書き出す</a>
  <input type="file" id="tpl-import-file" accept=".json,application/json" class="file-input">
  <label class="button" for="tpl-import-file">ファイルから読み込む</label>
  <span class="note">全テンプレートを JSON で書き出し/読み込みします。</span>
</div>
<dialog id="dlg-import" aria-labelledby="dlg-import-title">
  <h2 id="dlg-import-title">同名のテンプレートがあります</h2>
  <p>重複するテンプレート: <span id="dlg-import-names"></span></p>
  <form method="dialog">
    <div class="row" style="justify-content: flex-end">
      <button value="cancel" autofocus>キャンセル</button>
      <button value="rename">別名で追加</button>
      <button value="overwrite" class="danger">上書き</button>
    </div>
  </form>
</dialog>
<dialog id="dlg-print-placeholder" aria-labelledby="dlg-print-placeholder-title">
  <h2 id="dlg-print-placeholder-title">プレースホルダが含まれています</h2>
  <p><span id="dlg-print-placeholder-names"></span> はそのままの文字で印刷されます。データを差し込むには「差し込み印刷」を使ってください。</p>
  <form method="dialog">
    <div class="row" style="justify-content: flex-end">
      <button value="cancel" autofocus>キャンセル</button>
      <button value="print" class="danger">そのまま印刷する</button>
    </div>
  </form>
</dialog>
<dialog id="dlg-delete" aria-labelledby="dlg-delete-title">
  <h2 id="dlg-delete-title">テンプレートを削除しますか？</h2>
  <p>「<span id="dlg-delete-name"></span>」を削除します。元に戻せません。</p>
  <form method="dialog">
    <div class="row" style="justify-content: flex-end">
      <button value="cancel" autofocus>キャンセル</button>
      <button value="delete" class="danger">削除する</button>
    </div>
  </form>
</dialog>
</section>

<section class="card">
<h2 id="h-merge">差し込み印刷</h2>
<fieldset>
  <legend>データソース</legend>
  <div class="row">
    <label><input type="radio" name="src" value="csv" checked> CSV（行ごと）</label>
    <label><input type="radio" name="src" value="series"> 系列リスト</label>
  </div>
</fieldset>
<div id="src-csv">
  <div class="row">
    <div class="field" style="flex: 0 1 14em">
      <label for="csv-file">CSVファイル</label>
      <div><input type="file" id="csv-file" accept=".csv,text/csv"></div>
    </div>
    <div class="field" style="flex: 0 1 12em">
      <label for="csv-encoding">文字コード</label>
      <select id="csv-encoding">
        <option value="auto">自動判定</option>
        <option value="utf-8">UTF-8</option>
        <option value="shift_jis">Shift_JIS</option>
        <option value="euc-jp">EUC-JP</option>
      </select>
    </div>
    <span id="csv-encoding-status" role="status"></span>
  </div>
  <div class="field">
    <label for="csv-text">CSVデータ</label>
    <textarea id="csv-text" rows="5" placeholder="A,B&#10;aa,1&#10;bb,2&#10;cc,3"></textarea>
  </div>
  <p class="note">1行目がヘッダ（プレースホルダ名）、2行目以降が各ラベルです。文字コードは自動判定します。</p>
</div>
<div id="src-series" style="display:none">
  <fieldset>
    <legend>組み合わせ</legend>
    <div class="row">
      <label><input type="radio" name="mode" value="zip" checked> 対応付け</label>
      <label><input type="radio" name="mode" value="cross"> 組み合わせ</label>
      <button id="btn-detect" type="button">プレースホルダを検出</button>
      <button id="btn-series-add" type="button">系列を追加</button>
    </div>
  </fieldset>
  <div id="series-inputs"></div>
  <p class="note">各系列は1行1要素。対応付けは短い系列の長さに合わせます。追加した系列の名前はテキスト欄で {{名前}} として使います。</p>
</div>
<div class="row">
  <button id="btn-labels" type="button">ラベルを生成</button>
  <span id="labels-count" role="status"></span>
</div>
<div id="matrix"></div>
<div id="labels-list" class="labels-list" style="display:none"></div>
<div class="section-actions">
  <div class="group">
    <button id="btn-merge-print" type="button" class="primary">全ラベルを印刷</button>
  </div>
  <div class="group">
    <button id="btn-reset-merge" type="button" class="reset">差し込み印刷を初期設定に戻す</button>
  </div>
</div>
<div id="merge-progress" style="display:none">
  <div class="row">
    <progress id="merge-progress-bar" max="1" value="0" aria-label="差し込み印刷の進捗"></progress>
    <span id="merge-progress-text" role="status"></span>
    <button id="btn-merge-cancel" type="button">キャンセル</button>
  </div>
  <p class="note">全ラベルを 1 ジョブで送ります。送信後はキャンセルできません。</p>
</div>
<div id="merge-msg" class="msg" role="status"></div>
</section>

<section class="card">
<h2 id="h-history">印刷履歴</h2>
<div class="row">
  <button id="btn-history-refresh" type="button">履歴を更新</button>
  <button id="btn-history-clear" type="button">履歴を削除</button>
</div>
<div id="history-list"></div>
<p class="note">直近 100 件。「読み込む」で設定とテキストをフォームに戻します。「再印刷」はそのまま印刷します。</p>
<dialog id="dlg-history-clear" aria-labelledby="dlg-history-clear-title">
  <h2 id="dlg-history-clear-title">印刷履歴をすべて削除しますか？</h2>
  <p>削除した履歴は元に戻せません。</p>
  <form method="dialog">
    <div class="row" style="justify-content: flex-end">
      <button value="cancel" autofocus>キャンセル</button>
      <button value="clear" class="danger">すべて削除する</button>
    </div>
  </form>
</dialog>
</section>

<script>
const $ = id => document.getElementById(id);
let currentLabels = [];

function opts() {
  return {
    tape: $("tape").value,
    font: $("font").value,
    size: $("size").value,
    align: $("align").value,
    length_mm: $("length_mm").value,
    margin_preset: $("margin_preset").value,
    left_margin_mm: currentMargins()[0],
    right_margin_mm: currentMargins()[1],
    ratios: currentRatios(),
    frame: $("frame").checked,
    frame_width: $("frame_width").value,
    cut: $("cut").value,
    copies: $("copies").value,
    cable: $("cable").checked,
    cable_mm: $("cable_mm").value,
  };
}

// 余白プリセット (左, 右 mm)。「カスタム」のときは入力欄の値を使う。
const MARGIN_PRESETS = {none: ["1", "0"], xsmall: ["2", "1"], small: ["4", "3"], medium: ["6", "5"], large: ["8", "7"]};
function currentMargins() {
  const preset = $("margin_preset").value;
  if (preset in MARGIN_PRESETS) return MARGIN_PRESETS[preset];
  return [$("left_margin_mm").value, $("right_margin_mm").value];
}
// プリセット選択中は入力欄にその値を表示して無効化し、カスタムで有効にする。
// カスタムで入力した値はプリセットに切り替えても覚えておく。
let customMargins = ["4", "3"];
function syncMarginInputs() {
  const preset = $("margin_preset").value;
  const custom = preset === "custom";
  const [l, r] = custom ? customMargins : MARGIN_PRESETS[preset];
  $("left_margin_mm").value = l;
  $("right_margin_mm").value = r;
  $("left_margin_mm").disabled = !custom;
  $("right_margin_mm").disabled = !custom;
}
for (const id of ["left_margin_mm", "right_margin_mm"]) {
  $(id).addEventListener("input", () => {
    if ($("margin_preset").value === "custom") customMargins = [$("left_margin_mm").value, $("right_margin_mm").value];
  });
}
$("margin_preset").addEventListener("change", syncMarginInputs);
syncMarginInputs();
// テンプレート/履歴の復元用。プリセット名が無い古いデータは値から推定する。
function applyMargins(preset, left, right) {
  if (!preset && !left && !right) preset = "small";
  if (!preset) {
    preset = "custom";
    for (const [name, [l, r]] of Object.entries(MARGIN_PRESETS)) {
      if (String(left || l) === l && String(right || r) === r) { preset = name; break; }
    }
  }
  $("margin_preset").value = (preset in MARGIN_PRESETS || preset === "custom") ? preset : "small";
  if (preset === "custom") customMargins = [left || "4", right || "3"];
  syncMarginInputs();
}

// テキストの行数に合わせて比率入力欄を並べ直す (入力済みの値は保持)。
function syncRatioInputs() {
  const n = $("text").value.split("\n").length;
  const box = $("ratios-inputs");
  const prev = [...box.querySelectorAll("input")].map(i => i.value);
  box.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "0"; inp.step = "0.5"; inp.inputMode = "decimal";
    inp.setAttribute("aria-label", (i + 1) + "行目の比率");
    inp.setAttribute("aria-describedby", "ratios-note");
    inp.style.width = "3.5em"; inp.placeholder = "1";
    inp.value = prev[i] !== undefined ? prev[i] : "";
    inp.dataset.lineRatio = i;
    box.appendChild(inp);
    if (i < n - 1) box.appendChild(document.createTextNode(" : "));
  }
}
// 比率を "7,3" 形式で返す。全て空欄/1 なら "" (ドライバに渡さない)。
function currentRatios() {
  const vals = [...document.querySelectorAll("[data-line-ratio]")].map(i => i.value.trim());
  if (!vals.length || vals.every(v => v === "" || v === "1")) return "";
  return vals.map(v => v === "" ? "1" : v).join(",");
}
function setMsg(el, text, ok) {
  el.textContent = text || "";
  el.className = "msg " + (ok === true ? "ok" : ok === false ? "err" : "");
}
async function post(path, data) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  return r.json();
}

// 本体に装着されているテープ幅を取得して表示する。
async function detectTape() {
  const el = $("tape-status");
  el.textContent = "装着テープ: 確認中";
  el.className = "";
  try {
    const r = await (await fetch("/api/tape")).json();
    if (r.ok) {
      let s;
      if (r.tape_mm != null) {
        s = "装着テープ: " + r.tape_mm + "mm";
        if (!r.supported) s += "（この幅には印刷できません）";
      } else {
        s = "装着テープ: 印刷できない幅です (index " + r.index + ")";
      }
      if (r.device_id) {
        const mdl = (String(r.device_id).match(/MDL:([^;]+)/) || [])[1];
        if (mdl) s += " / 機種: " + mdl.trim();
      }
      if (r.mock) s += "（開発用の仮の値）";
      el.textContent = s;
      el.className = r.supported ? "ok" : "err";
    } else {
      el.textContent = "装着テープを確認できませんでした: " + r.error;
      el.className = "err";
    }
  } catch (e) {
    el.textContent = "装着テープを確認できませんでした: " + e;
    el.className = "err";
  }
}
// 印刷エリアの入力は「ケーブル表示ラベル」のときだけ有効にする。
function syncCableInput() {
  $("cable_mm").disabled = !$("cable").checked;
}
$("cable").addEventListener("change", syncCableInput);
syncCableInput();

$("btn-tape-detect").onclick = detectTape;
// テープ送り: 空白行だけのジョブを送る (カットなし / フルカット)。
async function feedTape(cut) {
  const el = $("feed-status");
  for (const id of ["btn-feed", "btn-feed-cut"]) $(id).disabled = true;
  el.className = ""; el.textContent = "送っています";
  try {
    const r = await post("/api/feed", {mm: $("feed_mm").value, cut});
    el.textContent = r.ok ? r.message : "テープを送れませんでした: " + r.error;
    el.className = r.ok ? "ok" : "err";
  } catch (e) {
    el.textContent = "テープを送れませんでした: " + e; el.className = "err";
  } finally {
    for (const id of ["btn-feed", "btn-feed-cut"]) $(id).disabled = false;
  }
}
$("btn-feed").onclick = () => feedTape("none");
$("btn-feed-cut").onclick = () => feedTape("full");

async function init() {
  const cfg = await (await fetch("/api/config")).json();
  // ドライバのパスや USB PID はサーバ設定の話なので画面には出さない。
  $("config").textContent = (cfg.dry_run ? "DRY_RUN モード (実機に送信しません) / " : "") +
    "機種: " + cfg.model + " (" + cfg.dpi + " dpi)";
  const fonts = (await (await fetch("/api/fonts")).json()).fonts;
  for (const f of fonts) {
    const o = document.createElement("option");
    o.value = f.file; o.textContent = f.name;
    $("font").appendChild(o);
  }
  await refreshTemplates();
  // フォントとテンプレートが揃ってから、前回の入力内容を戻す。
  restoreFormState();
  syncRatioInputs();
  detectTape();
  loadHistory();
}

// ------------------------------------------------------------------
// 入力内容の保存と復元 (localStorage)
// ブラウザ任せの復元 (F5) は JS で組み立てる欄が対象外で中途半端になる
// ため、全項目を明示的に保存して読み込み時に戻す。
// ------------------------------------------------------------------
const FORM_STATE_KEY = "tepra-web-form";
function collectFormState() {
  return Object.assign(opts(), {
    text: $("text").value,
    tpl_name: $("tpl-name").value,
    merge: {
      src: document.querySelector('input[name="src"]:checked').value,
      mode: document.querySelector('input[name="mode"]:checked').value,
      csv_text: $("csv-text").value,
      csv_encoding: $("csv-encoding").value,
      series: [...document.querySelectorAll(".series-block")].map(b => [
        b.querySelector("[data-series-name]").value, b.querySelector("[data-series-values]").value]),
    },
  });
}
let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { localStorage.setItem(FORM_STATE_KEY, JSON.stringify(collectFormState())); } catch (e) {}
  }, 200);
}
function restoreFormState() {
  let st = null;
  try { st = JSON.parse(localStorage.getItem(FORM_STATE_KEY) || "null"); } catch (e) { return; }
  if (!st || typeof st !== "object") return;
  applyFormState(st);
  $("tpl-name").value = st.tpl_name || "";
  const m = st.merge || {};
  for (const [name, value] of [["src", m.src || "csv"], ["mode", m.mode || "zip"]]) {
    const radio = document.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change")); }
  }
  $("csv-text").value = m.csv_text || "";
  if (m.csv_encoding) $("csv-encoding").value = m.csv_encoding;
  $("series-inputs").innerHTML = "";
  for (const [name, values] of m.series || []) addSeriesBlock(name, values);
}
document.addEventListener("input", scheduleSave);
document.addEventListener("change", scheduleSave);
new MutationObserver(scheduleSave).observe($("series-inputs"), {childList: true});
// ラベル欄を初期設定に戻す (差し込み印刷の入力は残す)。
$("btn-reset-label").onclick = () => {
  applyFormState({});
  $("tpl-name").value = "";
  clearPreview();
  setMsg($("msg"), "ラベルを初期設定に戻しました", true);
  scheduleSave();
};
// 差し込み印刷の入力と生成結果を初期設定に戻す (ラベル欄は残す)。
$("btn-reset-merge").onclick = () => {
  const csv = document.querySelector('input[name="src"][value="csv"]');
  csv.checked = true; csv.dispatchEvent(new Event("change"));
  document.querySelector('input[name="mode"][value="zip"]').checked = true;
  $("csv-text").value = "";
  $("csv-encoding").value = "auto";
  $("csv-file").value = "";
  csvFileB64 = null;
  $("csv-encoding-status").textContent = "";
  $("series-inputs").innerHTML = "";
  currentLabels = [];
  $("labels-count").textContent = "";
  $("labels-list").innerHTML = "";
  $("labels-list").style.display = "none";
  $("matrix").innerHTML = "";
  setMsg($("merge-msg"), "差し込み印刷を初期設定に戻しました", true);
  scheduleSave();
};
async function refreshTemplates() {
  const tpls = (await (await fetch("/api/templates")).json()).templates;
  const sel = $("tpl-list");
  sel.innerHTML = "";
  for (const name of Object.keys(tpls)) {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  }
  sel._data = tpls;
}

// プレビュー SVG と実寸表示・ルーラーを描く (単発/差し込み共通)。
let lastPreview = null;  // {feed_rows, length_mm, dpi}
function renderPreview(r) {
  const box = $("preview");
  box.innerHTML = '<div id="ruler" aria-hidden="true"></div>' + r.svg;
  $("preview-empty").hidden = true;
  lastPreview = {feed_rows: r.feed_rows, length_mm: r.length_mm, dpi: r.dpi || 360};
  const size = $("preview-size");
  if (r.length_mm != null) {
    size.textContent = "実寸: " + (r.tape_mm != null ? "約 " + r.length_mm + "mm × " + r.tape_mm + "mm（長さ × テープ幅）"
                                                       : "長さ 約 " + r.length_mm + "mm（テープ幅は自動）");
  } else {
    size.textContent = "";
  }
  $("ruler-note").style.display = r.length_mm != null ? "" : "none";
  requestAnimationFrame(drawRuler);
  // プレビュー欄はページ上部にあるので、見えるところまで戻す。
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("preview-section").scrollIntoView({behavior: reduce ? "auto" : "smooth", block: "start"});
}
function clearPreview() {
  $("preview").innerHTML = "";
  $("preview-size").textContent = "";
  $("ruler-note").style.display = "none";
  $("preview-empty").hidden = false;
}
// ルーラー: プレビュー SVG の表示幅に合わせ、5mm 刻み (10mm ごとに数値) の
// 目盛りを描く。SVG の 1 ドット = 1/dpi インチ (機種の解像度) として換算する。
function drawRuler() {
  const ruler = $("ruler");
  const svg = $("preview") ? $("preview").querySelector("svg") : null;
  if (!ruler || !svg || !lastPreview || !lastPreview.feed_rows) return;
  const totalMm = lastPreview.feed_rows * 25.4 / lastPreview.dpi;
  const w = svg.getBoundingClientRect().width;
  if (!w) return;
  ruler.style.width = w + "px";
  ruler.innerHTML = "";
  const pxPerMm = w / totalMm;
  for (let mm = 0; mm <= totalMm; mm += 5) {
    const tick = document.createElement("span");
    tick.className = "tick" + (mm % 10 === 0 ? " major" : "");
    tick.style.left = (mm * pxPerMm) + "px";
    ruler.appendChild(tick);
    if (mm % 10 === 0) {
      const lab = document.createElement("span");
      lab.className = "tick-label";
      lab.style.left = (mm * pxPerMm) + "px";
      lab.textContent = mm;
      ruler.appendChild(lab);
    }
  }
}
window.addEventListener("resize", drawRuler);

async function showPreview(text) {
  // 処理中はボタン無効化で示し、live region には結果だけを流す (途中状態は読み上げない)
  const btn = $("btn-preview");
  btn.disabled = true;
  setMsg($("msg"), "");
  try {
    const r = await post("/api/preview", {opts: opts(), text});
    if (r.ok) {
      renderPreview(r);
      setMsg($("msg"), (r.notes || []).join(" / "), true);
    } else {
      setMsg($("msg"), "プレビューを作成できませんでした: " + r.error, false);
    }
  } finally {
    btn.disabled = false;
  }
}

$("text").addEventListener("input", syncRatioInputs);

// 単発プレビュー用: テキスト内のプレースホルダ {{A}} を差し込みデータの
// 1 件目 (CSV の最初のデータ行 / 各系列の 1 番目) で置き換える。値が
// 見つからないプレースホルダがあればエラー文字列を返し、サーバへは送らない。
const PLACEHOLDER_RE_JS = /\{\{\s*([^\s{}]+)\s*\}\}/g;
function parseCsvRow(line) {
  // ダブルクォート対応の簡易 CSV 1 行パース (サーバの csv モジュールと同等の範囲)。
  const out = []; let cur = ""; let q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += c;
    } else if (c === '"') q = true;
    else if (c === ",") { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out;
}
function firstMergeValues() {
  const src = document.querySelector('input[name="src"]:checked').value;
  const values = {};
  if (src === "csv") {
    const lines = $("csv-text").value.replace(/^\ufeff/, "").split(/\r?\n/).filter(l => l.trim() !== "");
    if (lines.length >= 2) {
      const header = parseCsvRow(lines[0]).map(h => h.trim());
      const row = parseCsvRow(lines[1]);
      header.forEach((h, i) => { if (h && row[i] !== undefined) values[h] = row[i]; });
    }
  } else {
    document.querySelectorAll(".series-block").forEach(block => {
      const name = block.querySelector("[data-series-name]").value.trim();
      const first = block.querySelector("[data-series-values]").value.split("\n").map(s => s.trim()).find(s => s !== "");
      if (name && first !== undefined) values[name] = first;
    });
  }
  return {src, values};
}
function substituteFirstValues(text) {
  const names = [...new Set([...text.matchAll(PLACEHOLDER_RE_JS)].map(m => m[1]))];
  if (!names.length) return {text};
  const {src, values} = firstMergeValues();
  const missing = names.filter(n => !(n in values));
  if (missing.length) {
    const where = src === "csv" ? "CSV データ（1 行目ヘッダ + 2 行目）" : "系列リスト（各系列の 1 番目）";
    return {error: "プレースホルダ " + missing.map(n => "{{" + n + "}}").join(", ") + " の値が " + where + " にありません。差し込み印刷のデータを入力するか、プレースホルダを外してください。"};
  }
  return {text: text.replace(PLACEHOLDER_RE_JS, (_, n) => values[n]), substituted: names};
}

$("btn-preview").onclick = () => {
  const r = substituteFirstValues($("text").value);
  if (r.error) { setMsg($("msg"), r.error, false); return; }
  showPreview(r.text).then(() => {
    if (r.substituted && r.substituted.length && $("msg").classList.contains("ok")) {
      setMsg($("msg"), "プレースホルダ (" + r.substituted.map(n => "{{" + n + "}}").join(", ") + ") は 1 件目の値で表示しています", true);
    }
  });
};
async function printSingle() {
  const btn = $("btn-print");
  btn.disabled = true;
  setMsg($("msg"), "");
  try {
    const r = await post("/api/print", {opts: opts(), text: $("text").value});
    if (r.ok) setMsg($("msg"), r.message + (r.detail ? "\n" + r.detail : ""), true);
    else setMsg($("msg"), "印刷できませんでした: " + r.error, false);
    loadHistory();
  } finally {
    btn.disabled = false;
  }
}
// プレースホルダ入りのテキストをそのまま印刷しようとしたら警告して確認する。
$("btn-print").onclick = () => {
  const names = [...new Set([...$("text").value.matchAll(PLACEHOLDER_RE_JS)].map(m => m[1]))];
  if (!names.length) { printSingle(); return; }
  $("dlg-print-placeholder-names").textContent = names.map(n => "{{" + n + "}}").join(", ");
  setMsg($("msg"), "プレースホルダ " + names.map(n => "{{" + n + "}}").join(", ") + " は単発印刷では置き換えられません", false);
  $("dlg-print-placeholder").showModal();
};
$("dlg-print-placeholder").addEventListener("close", () => {
  const dlg = $("dlg-print-placeholder");
  const v = dlg.returnValue;
  dlg.returnValue = "";
  if (v === "print") printSingle();
  else setMsg($("msg"), "印刷していません", false);
});

$("btn-tpl-save").onclick = async () => {
  const name = $("tpl-name").value.trim();
  if (!name) { setMsg($("msg"), "保存するにはテンプレート名を入力してください", false); return; }
  const template = Object.assign(opts(), {text: $("text").value});
  await post("/api/templates", {name, template});
  await refreshTemplates();
  setMsg($("msg"), "テンプレート「" + name + "」を保存しました", true);
};
// 設定一式 (テンプレート / 履歴の opts + text) をフォームに流し込む。
function applyFormState(t) {
  $("tape").value = t.tape || "auto";
  $("font").value = t.font || "";
  $("size").value = t.size || "";
  $("align").value = t.align || "left";
  $("length_mm").value = t.length_mm || "";
  applyMargins(t.margin_preset, t.left_margin_mm, t.right_margin_mm);
  $("frame").checked = !!t.frame;
  $("frame_width").value = t.frame_width || "2";
  $("cut").value = t.cut || "full";
  $("copies").value = t.copies || "1";
  $("cable").checked = !!t.cable;
  $("cable_mm").value = t.cable_mm || "";
  syncCableInput();
  $("text").value = t.text || "";
  syncRatioInputs();
  if (t.ratios) {
    const parts = String(t.ratios).split(",");
    document.querySelectorAll("[data-line-ratio]").forEach((inp, i) => {
      inp.value = parts[i] !== undefined ? parts[i] : "";
    });
  }
}
$("btn-tpl-load").onclick = () => {
  const t = ($("tpl-list")._data || {})[$("tpl-list").value];
  if (!t) return;
  applyFormState(t);
  setMsg($("msg"), "テンプレートを読み込みました", true);
};
$("btn-tpl-delete").onclick = () => {
  const name = $("tpl-list").value;
  if (!name) { setMsg($("msg"), "削除するテンプレートを選んでください", false); return; }
  $("dlg-delete-name").textContent = name;
  $("dlg-delete").showModal();
};
$("dlg-delete").addEventListener("close", async () => {
  const dlg = $("dlg-delete");
  const v = dlg.returnValue;
  dlg.returnValue = "";  // 古い値が次のオープンに漏れないようにリセットする
  if (v !== "delete") return;
  const name = $("dlg-delete-name").textContent;
  await post("/api/templates/delete", {name});
  await refreshTemplates();
  setMsg($("msg"), "テンプレート「" + name + "」を削除しました", true);
});

document.querySelectorAll('input[name="src"]').forEach(r => {
  r.onchange = () => {
    const v = document.querySelector('input[name="src"]:checked').value;
    $("src-csv").style.display = v === "csv" ? "" : "none";
    $("src-series").style.display = v === "series" ? "" : "none";
  };
});
// CSV ファイルはバイト列のままサーバへ送り、文字コード判定込みで
// デコードしてテキストエリアへ反映する (Shift_JIS 等に対応するため)。
let csvFileB64 = null;
function bytesToB64(buf) {
  let s = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}
async function decodeCsvFile() {
  if (!csvFileB64) return;
  const enc = $("csv-encoding").value;
  const r = await post("/api/decode-csv", {data_b64: csvFileB64, encoding: enc});
  if (r.ok) {
    $("csv-text").value = r.text;
    const names = {"utf-8": "UTF-8", "shift_jis": "Shift_JIS", "euc-jp": "EUC-JP", "utf-16": "UTF-16"};
    $("csv-encoding-status").textContent =
      "文字コード: " + (names[r.encoding] || r.encoding) + (enc === "auto" ? "（自動判定）" : "（手動指定）");
    $("csv-encoding-status").className = "ok";
  } else {
    $("csv-encoding-status").textContent = "CSV を読み込めませんでした。文字コードを選び直してください: " + r.error;
    $("csv-encoding-status").className = "err";
  }
}
$("csv-file").onchange = async () => {
  const f = $("csv-file").files[0];
  if (!f) return;
  csvFileB64 = bytesToB64(await f.arrayBuffer());
  await decodeCsvFile();
};
$("csv-encoding").onchange = decodeCsvFile;
// 系列ブロック (名前入力 + 値テキストエリア + 削除ボタン) を1つ追加する。
function addSeriesBlock(name, values) {
  const box = $("series-inputs");
  const div = document.createElement("div");
  div.className = "series-block";
  const head = document.createElement("div");
  head.className = "row";
  const nameInp = document.createElement("input");
  nameInp.placeholder = "系列名";
  nameInp.setAttribute("aria-label", "系列名");
  nameInp.style.width = "8em";
  nameInp.value = name || "";
  nameInp.dataset.seriesName = "";
  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "この系列を削除";
  del.onclick = () => div.remove();
  head.appendChild(document.createTextNode("系列 {{"));
  head.appendChild(nameInp);
  head.appendChild(document.createTextNode("}}"));
  head.appendChild(del);
  const ta = document.createElement("textarea");
  ta.rows = 4;
  ta.setAttribute("aria-label", "系列の値（1行に1要素）");
  ta.placeholder = "1行に1要素";
  ta.value = values || "";
  ta.dataset.seriesValues = "";
  div.appendChild(head);
  div.appendChild(ta);
  box.appendChild(div);
}

$("btn-detect").onclick = () => {
  const names = [...new Set([...$("text").value.matchAll(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g)].map(m => m[1]))];
  const box = $("series-inputs");
  // 既存ブロックの入力値は名前で引き継ぐ
  const existing = {};
  box.querySelectorAll(".series-block").forEach(block => {
    const n = block.querySelector("[data-series-name]").value.trim();
    if (n) existing[n] = block.querySelector("[data-series-values]").value;
  });
  box.innerHTML = "";
  if (!names.length) {
    box.textContent = "テキストにプレースホルダがありません。{{A}} のように書くか、「系列を追加」で追加してください。";
    return;
  }
  for (const n of names) addSeriesBlock(n, existing[n] || "");
};

$("btn-series-add").onclick = () => {
  const used = new Set([...document.querySelectorAll("[data-series-name]")].map(i => i.value.trim()));
  let name = "";
  for (const c of "ABCDEFGHIJKLMNOPQRSTUVWXYZ") {
    if (!used.has(c)) { name = c; break; }
  }
  addSeriesBlock(name, "");
};

$("btn-labels").onclick = async () => {
  const src = document.querySelector('input[name="src"]:checked').value;
  let payload;
  if (src === "csv") {
    payload = {source: "csv", template: $("text").value, csv: $("csv-text").value};
  } else {
    const series = {};
    document.querySelectorAll(".series-block").forEach(block => {
      const name = block.querySelector("[data-series-name]").value.trim();
      if (!name) return;
      series[name] = block.querySelector("[data-series-values]").value
        .split("\n").map(s => s.trim()).filter(s => s !== "");
    });
    const mode = document.querySelector('input[name="mode"]:checked').value;
    payload = {source: "series", template: $("text").value, series, mode};
  }
  const r = await post("/api/labels", payload);
  if (!r.ok) { setMsg($("merge-msg"), "ラベルを生成できませんでした: " + (r.error || ""), false); return; }
  currentLabels = r.labels;
  let info = "生成: " + r.labels.length + " 件";
  if (r.meta && r.meta.lengths) {
    info += " (系列長: " + Object.entries(r.meta.lengths).map(([k, v]) => k + "=" + v).join(", ") + ")";
    if (r.meta.truncated) info += " ※対応付けのため短い方に合わせて切り詰めました";
  }
  $("labels-count").textContent = info;
  const list = $("labels-list");
  list.innerHTML = "";
  list.style.display = "";
  r.labels.forEach((label, i) => {
    const item = document.createElement("button");
    item.type = "button";
    item.textContent = (i + 1) + ": " + label.replace(/\n/g, " / ");
    item.onclick = async () => {
      const p = await post("/api/merge/preview", {opts: opts(), labels: currentLabels, index: i});
      if (p.ok) renderPreview(p);
      else setMsg($("merge-msg"), "プレビューを作成できませんでした: " + p.error, false);
    };
    list.appendChild(item);
  });
  const mx = $("matrix");
  mx.innerHTML = "";
  if (r.matrix) {
    // 系列名や値はユーザー入力なので、HTML 文字列にせず DOM API で組み立てる。
    const tbl = document.createElement("table");
    tbl.className = "matrix";
    const cellEl = (tag, textValue, scope) => {
      const el = document.createElement(tag);
      if (scope) el.scope = scope;
      el.textContent = textValue;
      return el;
    };
    const headRow = document.createElement("tr");
    headRow.appendChild(cellEl("th", r.matrix.row_name + " \\ " + r.matrix.col_name, "col"));
    for (const c of r.matrix.cols) headRow.appendChild(cellEl("th", c, "col"));
    tbl.appendChild(headRow);
    r.matrix.rows.forEach((rv, ri) => {
      const tr = document.createElement("tr");
      tr.appendChild(cellEl("th", rv, "row"));
      for (const cell of r.matrix.cells[ri]) tr.appendChild(cellEl("td", cell));
      tbl.appendChild(tr);
    });
    mx.appendChild(tbl);
  }
};

// 連続印刷はバックグラウンドジョブ。進捗をポーリングして表示し、
// キャンセルボタンで「印刷中のラベルが終わった時点」で停止できる。
let mergeJobId = null;
let mergePollTimer = null;
$("btn-merge-print").onclick = async () => {
  if (!currentLabels.length) { setMsg($("merge-msg"), "印刷するには先に「ラベルを生成」を押してください", false); return; }
  const btn = $("btn-merge-print");
  btn.disabled = true;
  setMsg($("merge-msg"), "");
  try {
    const r = await post("/api/merge/print", {opts: opts(), labels: currentLabels});
    if (!r.ok) { setMsg($("merge-msg"), "印刷を開始できませんでした: " + (r.error || ""), false); btn.disabled = false; return; }
    mergeJobId = r.job_id;
    $("merge-progress").style.display = "";
    $("merge-progress-bar").max = r.total;
    $("merge-progress-bar").value = 0;
    $("merge-progress-text").textContent = "0 / " + r.total + " 件";
    $("btn-merge-cancel").disabled = false;
    mergePollTimer = setInterval(pollMergeJob, 500);
    pollMergeJob();
  } catch (e) {
    setMsg($("merge-msg"), "印刷を開始できませんでした: " + e, false);
    btn.disabled = false;
  }
};
async function pollMergeJob() {
  if (!mergeJobId) return;
  let r;
  try {
    r = await (await fetch("/api/merge/status?job=" + encodeURIComponent(mergeJobId))).json();
  } catch (e) { return; }
  if (!r.ok) return;
  $("merge-progress-bar").max = r.total;
  $("merge-progress-bar").value = r.done;
  let text = r.done + " / " + r.total + " 件";
  if (r.state === "running" && r.current_label != null) {
    text += " — 印刷中: " + (r.current_index + 1) + " 件目「" + String(r.current_label).replace(/\n/g, " / ") + "」";
  } else if (r.state === "running" && r.phase) {
    text += " — " + r.phase;
  }
  $("merge-progress-text").textContent = text;
  if (r.state !== "running") {
    clearInterval(mergePollTimer);
    mergePollTimer = null;
    mergeJobId = null;
    $("btn-merge-print").disabled = false;
    $("btn-merge-cancel").disabled = true;
    if (r.state === "done") setMsg($("merge-msg"), r.done + " 件を印刷しました" + (r.detail ? "\n" + r.detail : ""), true);
    else if (r.state === "cancelled") setMsg($("merge-msg"), "送信前に取り消しました。印刷していません", false);
    else setMsg($("merge-msg"), "印刷できませんでした: " + r.error, false);
    loadHistory();
  }
}
$("btn-merge-cancel").onclick = async () => {
  if (!mergeJobId) return;
  $("btn-merge-cancel").disabled = true;
  await post("/api/merge/cancel", {job_id: mergeJobId});
};

// ------------------------------------------------------------------
// テンプレートの読み込み (インポート)
// ------------------------------------------------------------------
let pendingImport = null;
$("tpl-import-file").onchange = async () => {
  const f = $("tpl-import-file").files[0];
  $("tpl-import-file").value = "";
  if (!f) return;
  let parsed;
  try {
    parsed = JSON.parse(await f.text());
  } catch (e) {
    setMsg($("msg"), "テンプレートを読み込めませんでした。JSON ファイルではありません", false);
    return;
  }
  const incoming = parsed.templates && typeof parsed.templates === "object" ? parsed.templates : parsed;
  if (!incoming || typeof incoming !== "object" || Array.isArray(incoming)) {
    setMsg($("msg"), "テンプレートを読み込めませんでした。このアプリで書き出したファイルを選んでください", false);
    return;
  }
  const existing = ($("tpl-list")._data) || {};
  const conflicts = Object.keys(incoming).filter(n => n in existing);
  if (!conflicts.length) {
    await doImport(incoming, "rename");
  } else {
    pendingImport = incoming;
    $("dlg-import-names").textContent = conflicts.join("、");
    $("dlg-import").showModal();
  }
};
async function doImport(incoming, strategy) {
  const r = await post("/api/templates/import", {templates: incoming, strategy});
  if (!r.ok) { setMsg($("msg"), "テンプレートを読み込めませんでした: " + (r.error || ""), false); return; }
  await refreshTemplates();
  let msg = r.imported.length + " 件のテンプレートを読み込みました";
  const ren = Object.entries(r.renamed || {});
  if (ren.length) msg += "（別名で追加: " + ren.map(([a, b]) => a + " → " + b).join("、") + "）";
  setMsg($("msg"), msg, true);
}
$("dlg-import").addEventListener("close", async () => {
  const dlg = $("dlg-import");
  const v = dlg.returnValue;
  dlg.returnValue = "";
  const incoming = pendingImport;
  pendingImport = null;
  if (!incoming || v === "cancel" || v === "") return;
  await doImport(incoming, v === "overwrite" ? "overwrite" : "rename");
});

// ------------------------------------------------------------------
// 印刷履歴
// ------------------------------------------------------------------
async function loadHistory() {
  const list = $("history-list");
  let hist;
  try {
    hist = (await (await fetch("/api/history")).json()).history || [];
  } catch (e) { return; }
  list.innerHTML = "";
  if (!hist.length) {
    list.innerHTML = '<p class="note">履歴はまだありません。印刷するとここに残ります。</p>';
    return;
  }
  for (const entry of hist) {
    const row = document.createElement("div");
    row.className = "history-item";
    const time = document.createElement("span");
    time.className = "h-time";
    time.textContent = (entry.ts || "").replace("T", " ").slice(0, 19);
    const kind = document.createElement("span");
    kind.className = "h-kind";
    kind.textContent = entry.kind === "merge" ? "差し込み" : "単発";
    const text = document.createElement("span");
    text.className = "h-text";
    if (entry.kind === "merge") {
      const labels = entry.labels || [];
      text.textContent = labels.length + " 件: " + (labels[0] || "").replace(/\n/g, " / ") + (labels.length > 1 ? " …" : "");
    } else {
      text.textContent = (entry.text || "").replace(/\n/g, " / ");
    }
    const status = document.createElement("span");
    status.className = "h-status";
    if (entry.ok) {
      status.textContent = "成功" + (entry.kind === "merge" ? " (" + entry.done + "/" + entry.total + ")" : "");
      status.classList.add("ok");
    } else if (entry.state === "cancelled") {
      status.textContent = "キャンセル (" + entry.done + "/" + entry.total + ")";
      status.classList.add("err");
    } else {
      let errText = entry.error || "";
      status.title = errText;
      if (errText.length > 140) errText = errText.slice(0, 140) + "…";
      status.textContent = "失敗" + (entry.kind === "merge" ? " (" + entry.done + "/" + entry.total + ")" : "") +
        (errText ? ": " + errText : "");
      status.classList.add("err");
    }
    const shortText = text.textContent.length > 30 ? text.textContent.slice(0, 30) + "…" : text.textContent;
    const load = document.createElement("button");
    load.type = "button";
    load.textContent = "読み込む";
    load.setAttribute("aria-label", "読み込む: " + shortText);
    load.onclick = () => loadHistoryEntry(entry);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "再印刷";
    btn.setAttribute("aria-label", "再印刷: " + shortText);
    btn.onclick = () => reprintHistory(entry.id, btn);
    row.append(time, kind, text, status, load, btn);
    list.appendChild(row);
  }
}
// 履歴の設定とテキストをフォームに戻す (印刷はしない)。差し込み印刷の
// 履歴は元の CSV / 系列が保存されていないので、設定と先頭ラベルのみ。
function loadHistoryEntry(entry) {
  const opts = Object.assign({}, entry.opts || {});
  if (entry.kind === "merge") {
    const labels = entry.labels || [];
    applyFormState(Object.assign(opts, {text: labels[0] || ""}));
    setMsg($("msg"), "設定と 1 件目のラベル（全 " + labels.length + " 件）を読み込みました。元の CSV と系列は履歴に残っていません", true);
  } else {
    applyFormState(Object.assign(opts, {text: entry.text || ""}));
    setMsg($("msg"), "履歴から読み込みました", true);
  }
  $("text").focus();
  $("h-label").scrollIntoView({behavior: "smooth", block: "start"});
}
async function reprintHistory(id, btn) {
  btn.disabled = true;
  try {
    const r = await post("/api/history/reprint", {id});
    if (!r.ok) { setMsg($("msg"), "再印刷できませんでした: " + (r.error || ""), false); return; }
    if (r.async) {
      // 差し込みの再印刷は進捗表示つきジョブとして開始する
      mergeJobId = r.job_id;
      currentLabels = currentLabels; // 表示用ラベル一覧は変えない
      $("merge-progress").style.display = "";
      $("merge-progress-bar").max = r.total;
      $("merge-progress-bar").value = 0;
      $("merge-progress-text").textContent = "0 / " + r.total + " 件";
      $("btn-merge-cancel").disabled = false;
      $("btn-merge-print").disabled = true;
      mergePollTimer = setInterval(pollMergeJob, 500);
      pollMergeJob();
      window.scrollTo({top: document.body.scrollHeight, behavior: "smooth"});
    } else {
      setMsg($("msg"), r.message || "再印刷しました", true);
      loadHistory();
    }
  } finally {
    btn.disabled = false;
  }
}
$("btn-history-refresh").onclick = loadHistory;
$("btn-history-clear").onclick = () => $("dlg-history-clear").showModal();
$("dlg-history-clear").addEventListener("close", async () => {
  const dlg = $("dlg-history-clear");
  const v = dlg.returnValue;
  dlg.returnValue = "";
  if (v !== "clear") return;
  await post("/api/history/clear", {});
  await loadHistory();
  setMsg($("msg"), "印刷履歴を削除しました", true);
});

init();
</script>
</main>
</body>
</html>
"""


def main():
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"TEPRA Web: http://{HOST}:{PORT}/  driver={DRIVER} model={MODEL} dry_run={DRY_RUN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
