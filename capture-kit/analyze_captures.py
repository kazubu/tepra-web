#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TEPRA 印刷ジョブ (.prn) キャプチャの比較解析スクリプト。

Windows 公式ドライバの FILE: ポート出力で集めた .prn ファイル群を読み、
ジョブ構造 (ヘッダ/設定コマンド/ラスタ/コピー) を分解して一覧化し、
「テープ幅だけが違うペア」「カット設定だけが違うペア」の差分オフセットから
テープ幅バイト / カットバイトの候補を指摘する。

使い方:
    python3 analyze_captures.py <ディレクトリまたは .prn ファイル...>

ファイル命名規則 (README.md 参照):
    w<幅mm>_<カット>.prn     例: w36_full.prn, w18_half.prn, w12_none.prn
    カットは full (フルカット) / half (ハーフカット) / none (カットなし)。
    規則に合わないファイルも構造解析の表には載せるが、候補の指摘からは外れる。

ジョブ形式 (driver/docs/protocol.md、2026-11-15 の SR3900P キャプチャで確定):
    - ヘッダ: 先頭 2179 バイト固定長。offset 8 = テープ幅バイト (mm そのまま)、
      offset 23 = 補助バイト (幅ごとの既知の値)。
    - 設定コマンド: ESC { len cmd params... checksum } フレーム。
      C (4 バイト: カット設定) D (1 バイト) G (0 バイト)。
    - コピーごとに L (行数, u32 LE) T (先行マージン行数, u16 LE) と
      ラスタ行、末尾 0x0C。コピーが複数あれば L から繰り返す。
    - ラスタ 1 行 = ESC . 00 xx xx 01 <ドット数 u16 LE> <ビットマップ ドット数/8 バイト>。
    - フッタ: '@' フレーム 6 バイト。
"""

import re
import struct
import sys
from pathlib import Path

HEADER_LEN = 2179
NAME_RE = re.compile(r"^w(\d+)_(full|half|none)(?:_.+)?\.prn$", re.IGNORECASE)

# 既知のオフセット (先行調査で判明しているもの)
KNOWN_OFFSETS = {
    8: "テープ幅バイト (mm) — 既知",
    9: "ドライバ依存 (SR3700P 版 0x0E / SR3900P 版 0x68) — 幅・カットとは無関係",
    20: "文字数依存 (12 + 4 × 文字数) — 既知",
    23: "補助バイト (幅依存: 6mm=3, 9mm=4, 12mm=5, 18mm=6, 36mm=8 が実測、24mm=7 は補間) — 既知",
    2177: "ヘッダのチェックサム — 既知 (中身が変われば必ず変わる)",
    2183: "C コマンド param0 (カット: full=3, half=2, none=0) — 既知",
    2184: "C コマンド param1 (カット: full=1, half=2, none=0) — 既知",
    2185: "C コマンド param2 (カット: none=0, それ以外 1) — 既知",
    2186: "C コマンド param3 (カット: none=0, それ以外 1) — 既知",
    2187: "C コマンドのチェックサム — 既知",
}
CUT_BY_C_PARAMS = {
    (3, 1, 1, 1): "full",
    (2, 2, 1, 1): "half",
    (0, 0, 0, 0): "none",
}


def parse_job(data: bytes) -> dict:
    """ジョブをフレーム単位で歩いて構造を返す。"""
    info = {"size": len(data), "ok": False, "copies": []}
    if len(data) < HEADER_LEN + 10:
        info["error"] = "ファイルが短すぎます"
        return info
    if data[0:3] != b"\x1b{\x80":
        info["error"] = "先頭がヘッダフレーム (1B 7B 80) ではありません"
        return info
    info["tape_byte"] = data[8]
    info["aux_byte"] = data[23]
    info["version_tag"] = data[0x61:0x61 + 6].decode("utf-16-le", "replace")
    pos = HEADER_LEN
    cmds = {}
    current = None
    row_prefix = None
    while pos < len(data):
        b = data[pos]
        if b == 0x1B and pos + 1 < len(data) and data[pos + 1] == 0x7B:
            ln = data[pos + 2]
            cmd = chr(data[pos + 3])
            params = data[pos + 4:pos + 3 + ln - 2]
            if cmd == "L":
                current = {"rows": 0, "row_len": None, "margin": None,
                           "row_count": struct.unpack("<I", params)[0]}
                info["copies"].append(current)
            elif cmd == "T" and current is not None:
                current["margin"] = struct.unpack("<H", params)[0]
            elif cmd == "@":
                info["footer_at"] = pos
            else:
                cmds[cmd] = bytes(params)
            pos += ln + 3
            continue
        if b == 0x1B and pos + 1 < len(data) and data[pos + 1] == 0x2E:
            if current is None:
                info["error"] = f"offset {pos}: L コマンドの前にラスタ行があります"
                return info
            dots = struct.unpack("<H", data[pos + 6:pos + 8])[0]
            row_len = 8 + (dots + 7) // 8
            prefix = data[pos + 2:pos + 6].hex(" ")
            if row_prefix is None:
                row_prefix = prefix
            elif row_prefix != prefix:
                row_prefix = row_prefix + " (混在)"
            current["dots"] = dots
            current["row_len"] = row_len
            current["rows"] += 1
            pos += row_len
            continue
        if b == 0x0C:
            pos += 1
            continue
        info["error"] = f"offset {pos}: 想定外のバイト 0x{b:02X}"
        return info
    info["cmds"] = cmds
    info["row_prefix"] = row_prefix
    c = tuple(cmds.get("C", b""))
    info["c_params"] = c
    info["cut"] = CUT_BY_C_PARAMS.get(c, "不明")
    for cp in info["copies"]:
        if cp["rows"] != cp["row_count"]:
            info["error"] = f"L={cp['row_count']} に対しラスタ行が {cp['rows']} 行"
            return info
    if not info["copies"]:
        info["error"] = "ラスタが見つかりません"
        return info
    first = info["copies"][0]
    info["row_count"] = first["row_count"]
    info["margin_rows"] = first["margin"]
    info["dots"] = first["dots"]
    info["bitmap_bytes"] = first["row_len"] - 8
    info["ok"] = True
    return info


def diff_offsets(a: bytes, b: bytes, limit: int) -> list:
    """先頭 limit バイトの範囲で異なるオフセットを返す。"""
    n = min(limit, len(a), len(b))
    return [i for i in range(n) if a[i] != b[i]]


def fmt_offsets(offs, a, b, max_show=40):
    parts = []
    for i in offs[:max_show]:
        note = f" ({KNOWN_OFFSETS[i]})" if i in KNOWN_OFFSETS else ""
        parts.append(f"  offset {i}: 0x{a[i]:02X} -> 0x{b[i]:02X}{note}")
    if len(offs) > max_show:
        parts.append(f"  ... 他 {len(offs) - max_show} 件")
    return "\n".join(parts)


def main(argv):
    paths = []
    for arg in argv[1:]:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.prn")))
        elif p.suffix == ".prn":
            paths.append(p)
    if not paths:
        print("使い方: python3 analyze_captures.py <ディレクトリまたは .prn ファイル...>")
        return 2

    jobs = {}   # path -> (data, info, width, cut)
    print("=== ジョブ構造の一覧 ===")
    print(f"{'ファイル':24} {'サイズ':>7} {'幅':>3} {'補助':>4} {'ドット':>5} {'bitmap':>6} "
          f"{'行数':>5} {'余白':>4} {'コピー':>4} {'カット(C)':>18} {'行prefix':>12} {'版':>4}  命名")
    for p in paths:
        data = p.read_bytes()
        info = parse_job(data)
        m = NAME_RE.match(p.name)
        width = int(m.group(1)) if m else None
        cut = m.group(2).lower() if m else None
        jobs[p.name] = (data, info, width, cut)
        naming = f"w{width} / {cut}" if m else "(命名規則外)"
        if info.get("ok"):
            cparams = " ".join(f"{x:02X}" for x in info["c_params"])
            print(f"{p.name:24} {info['size']:>7} {info['tape_byte']:>3} {info['aux_byte']:>4} "
                  f"{info['dots']:>5} {info['bitmap_bytes']:>6} {info['row_count']:>5} "
                  f"{info['margin_rows']:>4} {len(info['copies']):>4} "
                  f"{info['cut'] + ' ' + cparams:>18} {info['row_prefix']:>12} {info['version_tag'].strip(chr(0)):>4}  {naming}")
            if cut and info["cut"] != cut:
                print(f"    ※ 命名のカット ({cut}) と C パラメータ ({info['cut']}) が一致しません")
        else:
            print(f"{p.name:24} {info['size']:>7}  解析エラー: {info.get('error')}  {naming}")

    named = {n: v for n, v in jobs.items() if v[2] is not None and v[1].get("ok")}
    by_key = {(v[2], v[3]): n for n, v in named.items()}
    cmp_limit = HEADER_LEN + 10 + 7 + 6   # ヘッダ + C/D/G (L/T 以降はラベル長依存)

    print()
    print("=== カット設定だけが違うペア (カットバイト候補) ===")
    found_cut = False
    widths = sorted({w for w, _c in by_key})
    for w in widths:
        cuts = [c for (ww, c) in by_key if ww == w]
        for i in range(len(cuts)):
            for j in range(i + 1, len(cuts)):
                a_name = by_key[(w, cuts[i])]
                b_name = by_key[(w, cuts[j])]
                a, b = jobs[a_name][0], jobs[b_name][0]
                offs = diff_offsets(a, b, cmp_limit)
                found_cut = True
                print(f"{a_name} vs {b_name}: ヘッダ/設定部の差分 {len(offs)} 件")
                print(fmt_offsets(offs, a, b) if offs else "  (差分なし — カット設定はヘッダ外か、未反映の可能性)")
    if not found_cut:
        print("(同じ幅でカット設定の違うペアがありません)")

    print()
    print("=== テープ幅だけが違うペア (幅バイト候補) ===")
    found_w = False
    cuts_all = sorted({c for _w, c in by_key})
    for c in cuts_all:
        ws = sorted(w for (w, cc) in by_key if cc == c)
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                a_name = by_key[(ws[i], c)]
                b_name = by_key[(ws[j], c)]
                a, b = jobs[a_name][0], jobs[b_name][0]
                offs = diff_offsets(a, b, cmp_limit)
                found_w = True
                print(f"{a_name} vs {b_name}: ヘッダ/設定部の差分 {len(offs)} 件")
                print(fmt_offsets(offs, a, b) if offs else "  (差分なし)")
    if not found_w:
        print("(同じカット設定で幅の違うペアがありません)")

    print()
    print("=== 読み方 ===")
    print("- 幅バイト (offset 8)、補助バイト (offset 23)、ドット数 (ラスタ行ヘッダの u16)、")
    print("  カット (C コマンドの 4 パラメータ) は確定済みです。表の値がドライバの")
    print("  TapeWidth / CutMode と一致するかを確認してください。")
    print("- 未知の幅 (4mm / 50mm 等) を採取したら、補助バイトとドット数をドライバの")
    print("  src/tepra/mod.rs に追加します。")
    print("- offset 9 と版文字列 (offset 0x61 の \"306\"/\"500\") は公式ドライバの版で")
    print("  変わる値で、幅・カットには関係ありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
