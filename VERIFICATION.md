# 動作確認記録 (2026-11-15, 実機なし環境)

## ドライバ (driver/)

- `cargo build` / `cargo build --release`: 成功 (Rust 1.100)
- `cargo test`: **39 passed, 0 failed**
- ジョブ生成: `./tpsr3700 --tape-mm 18 --no-print --skip-preview --dump job.prn "Hello TEPRA"`
  → 38,158 bytes のジョブ生成。ヘッダのテープ幅バイト = 0x12 (18mm)。
  ジョブのラスタ部分をパースして描画したところ "Hello TEPRA" の
  文字形が正しくレンダリングされていることを確認。
- プレビュー: `--tape-mm 12 --preview-out prev.svg "テスト\n2行目"`
  → SVG (8,361 bytes) をプロンプトなしで生成。USB にはアクセスしない。
- PID 差し替え: `--pid 0103` で 0x0103 として探索 (エラーメッセージで確認)。
  `TEPRA_USB_PID=abcd` で 0xabcd として探索。どちらも「見つからない」
  エラーに探した ID が表示されることを確認。

## Web アプリ (server.py, TEPRA_DRY_RUN=1 で起動して確認)

- `GET /` → 200、`GET /api/config` / `/api/fonts` 正常応答。
  フォント一覧に fonts/ 内の BIZUDPGothic-Regular.ttf が列挙される。
- `POST /api/preview` → ok、SVG に描画パスを含む
  (印刷と同じ描画経路である CLI の --preview-out を使用)。
- `POST /api/print` (DRY_RUN) → ジョブ生成 26,674 bytes の応答。
- 差し込み (系列リスト):
  - zip: A=[aa,bb,cc], B=[1,2,3], テンプレート `{{A}} {{B}}`
    → `aa 1` `bb 2` `cc 3` の **3件** (期待通り)
  - cross: 同条件 → `aa 1` `aa 2` `aa 3` `bb 1` `bb 2` `bb 3`
    `cc 1` `cc 2` `cc 3` の **9件**、順序も期待通り。
    マトリクス表示用データ (行=A, 列=B, セル=ラベル) も正しく生成。
  - zip で長さ違い (A=2件, B=3件) → 2件に切り詰め + truncated フラグ表示。
  - CSV モード: ヘッダ name の CSV → 行ごとにラベル生成を確認。
- 連続印刷 (DRY_RUN): zip 3件 → done=3、cross 9件 → done=9。
  失敗ケース (存在しないフォント指定) → done=0 / failed_index=0 /
  failed_label / エラーメッセージが返ることを確認。
- テンプレート保存 → 一覧に反映されることを確認。

## 行ごとのサイズ比率 / 用語統一 / 系列追加 (2026-11-15 追加分)

### ドライバ (`--line-ratios`)

- `cargo build --release` 成功、`cargo test` **48 passed, 0 failed**
  (従来 39 件 + 比率関連 9 件: サイズ計算 3、バンド分割 1、
  バリデーション 1、等比率=従来描画とのバイト一致 1、7:3 描画形状 1、
  CLI パーサ 2)。
- 数値検証: `HOST\n192.168.0.1`、18mm、サイズ自動で dump ジョブを
  生成し、ラスタをパースして行ごとのインク高さ (ドット) と
  ラベル長を計測した。
  - 1:1 → 1行目 69 / 2行目 72 ドット、ラベル長 630 行 (約 76.9mm)
  - 7:3 → 1行目 98 / 2行目 43 ドット (インク高さ比 約 2.28 ≒ 7/3)、
    ラベル長 464 行 (約 56.7mm)。長い IP 行が縮み、ラベル全体が
    約 26% 短くなった。
  - `--preview-out` の SVG も同一設定で生成できる (7:3 で 14,474 bytes、
    Web API 経由のプレビューとバイト数一致)。
- エラー系: 比率個数≠行数 / 0 の比率 / 折り返し
  (`--max-tape-length-mm`) との併用は、いずれも終了コード 1 で
  明示的なエラーメッセージになることを確認。

### Web アプリ

- 比率入力欄はテキストの行数に応じて自動生成される。
  /api/preview・/api/print・差し込み (個別プレビュー/連続印刷) の
  全経路で ratios が CLI の `--line-ratios` に渡ることを DRY_RUN で
  確認。テンプレート保存にも比率が含まれる (保存→取得の往復で確認)。
- 用語統一: 組み合わせモードの表示を「対応付け」「組み合わせ」に
  変更した (内部値 zip/cross は不変)。
- 系列追加: 「系列を追加」で名前付きの系列を増やせ、各系列は削除も
  できる。3 系列の組み合わせ (2×2×2 = 8 件、順序は直積順) と
  対応付け (最短系列への切り詰め) を Python テストと HTTP 経由の
  両方で確認。マトリクス表は 2 系列の組み合わせのときだけ表示され、
  3 系列以上では一覧表示のみになる。
- スクリーンショット: `ui-review/05-ratios.png` (比率入力欄)、
  `06-ratio-preview.png` (HOST/IP の 7:3 プレビュー)、
  `07-series.png` (系列追加 + 3 系列の組み合わせ生成)。

## 装着テープ幅の取得 (2026-11-15 追加分)

### ドライバ (`--detect-tape`)

- `cargo build --release` 成功、`cargo test` **52 passed, 0 failed**
  (従来 48 件 + 検出関連 4 件: インデックス表、JSON 生成の
  対応幅/未対応幅/未知インデックス)。
- 実機なし環境でのエラー経路を確認:
  - `--detect-tape` → 終了コード 1、標準出力は空、標準エラーに
    「looking for vendor=0x0d8a product=0x0103」と探した ID を表示。
  - `--detect-tape --pid abcd` → product=0xabcd で探索 (エラーで確認)。
  - `TEPRA_USB_PID=3900` → product=0x3900 で探索 (エラーで確認)。
  - 成功時の JSON 出力はユニットテストで担保 (実機での検出成功
    そのものは未確認。下記「確認できていないこと」参照)。

### Web アプリ (`GET /api/tape`)

- `TEPRA_FAKE_TAPE=18` (開発用モック) で起動し、`/api/tape` が
  `{"ok": true, "tape_mm": 18, "index": 4, "supported": true, "mock": true}`
  を返すことを確認。`/api/config` にも `fake_tape` が出る。
- モックなし・実機なしでは `/api/tape` が
  `{"ok": false, "error": "Error: TEPRA printer not found (looking for ...)"}`
  を返し、UI にはその理由がそのまま表示される (正直な失敗)。
- UI: 「本体から取得」ボタンと結果表示を追加。ページ読み込み時に
  自動で 1 回取得する。スクリーンショット `ui-review/08-tape.png`
  はモック有効時の表示 (「装着テープ: 18mm（開発用モック表示）」)。

## 確認できていないこと

- SR3900P 実機での印刷 (実機なし)。USB PID も未確定。
- テープ幅取得の実機での検出成功 (SR3700P 系では上流の印刷フローで
  実績のある問い合わせだが、本パッケージの SR3900P では未確認)。
  インデックスが返ること自体は実機接続時に初めて分かる。
- SR3700P の 4 / 9 / 24 / 36mm のジョブ生成は推定値 (未検証)。
- SR3900P の全幅 (4〜36mm): SR3900P 公式ドライバのキャプチャとバイト一致
  (下記「SR3900P キャプチャ対応」)。実機印刷は 9mm のみ確認 (送信直後に
  印刷、上下中央)。
- カット方式 (full / half / none) と枚数 (`--copies`): SR3900P 実機で
  動作確認済み (2026-11-15)。

## UI 文言・実装の見直し (2026-11-15、ux-writing / modern-web-guidance スキル適用)

- ux-writing: ボタンは「動詞 + 対象」で区別できる名前に (「読み込み」「削除」→
  「選択したテンプレートを読み込む / 削除」、「本体から取得」→「装着テープを
  確認」、「連続印刷」→「全ラベルを印刷」、「履歴をクリア」→「履歴を削除」)。
  エラーは「何ができなかったか + 原因/対処」の順 (「エラー: …」を全廃)。
  空状態に次の行動を添える。説明文は 1 文 14 語前後に短縮。確認ダイアログは
  結果を 1 文で。
- modern-web-guidance (html / accessibility / forms / dialog / dark-mode を参照):
  `<main>` ランドマーク追加、`<section aria-labelledby>` の region 化を解除、
  `title` ツールチップ廃止 (キーボードで読めない) → `aria-describedby` +
  visually-hidden、履歴行の同名ボタンに `aria-label` で区別、フォーム部品に
  `font-size: max(1rem, 1em)` (iOS ズーム防止)、ラベル代わりの placeholder を
  例示のみに。ダイアログは既に `<dialog>` + `showModal()` + 安全側 autofocus +
  `returnValue` リセットで準拠、ダークモードは `color-scheme` meta + `:root` +
  `light-dark()` + フォールバックで準拠。
- 確認: ページ内 JS の構文チェック、DRY_RUN でページ配信とプレビュー API。

## SR3900P キャプチャ対応 (2026-11-15 追加分)

- 入力: `capture-kit/1x`, `2x` (9mm / 36mm × full / half / none ×
  1 枚 / 2 枚、SR3900P 公式ドライバ、"TEST 123")。
- `analyze_captures.py` (書き直し後) の構造表: 9mm = 幅 9 / 補助 4 /
  112 ドット / 14 バイト、36mm = 幅 36 / 補助 8 / 384 ドット / 48 バイト、
  行 prefix `00 0a 0a 01`、版 `500`。カットは C パラメータで full
  `03 01 01 01` / half `02 02 01 01` / none `00 00 00 00`。2 枚は
  L/T/ラスタ/0x0C の繰り返し。
- ドライバ: `cargo test --release` **66 passed, 0 failed**。うち
  キャプチャ 7 本 (`tests/fixtures/sr3900p-*.prn`) との一致テスト
  (ヘッダ offset 32 とチェックサム以外の全バイト) と、上流の SR3700P
  キャプチャ 6 本の完全一致テストを両立。
- 生成確認: `--model sr3900p --tape-mm 36 --cut none`、
  `--tape-mm 9 --cut half --copies 2` のダンプを analyze_captures.py
  で解析し、キャプチャと同じ構造 (prefix `0a 0a`、版 500、C パラメータ、
  コピー数) になることを確認。
- 追加キャプチャ (`1x/w4,w6,w12,w18,w24_half.prn`) で SR3900P の
  ドット数 48/72/144/216/288 と補助バイト 18/3/5/6/7 を確定。
  `tests/fixtures/sr3900p-w{4,6,12,18,24}-half.prn` との一致テストを追加
  (`cargo test --release` 68 passed)。
- Web: DRY_RUN + `TEPRA_FAKE_TAPE=36` で /api/tape が 36mm を対応扱い、
  36mm + ハーフカット + 2 枚のプレビュー生成、18mm プレビューの注記に
  warning が出ることを確認。
- 印刷開始遅延: `capture-kit/usbcap_9mm_half.pcapng` (SR3900P ドライバの
  印刷 1 回分) を解析し、ヘッダブロック非送信・SOFT_RESET・状態
  ポーリングを確認して実装 (PATCH_NOTES.md 7 節)。`printer_payload`
  がキャプチャの bulk データ長 (10014 バイト) と一致するテストを追加。
  `cargo test --release` **67 passed**。SR3900P 実機で 9mm を
  `--wait` 付きで印刷し、送信直後に印刷が始まることを確認 (2026-11-15)。
- フォント pt 換算: キャプチャが MS ゴシック 16pt と分かったため、半角
  ピッチ (0.5 em) から em = pt × 1.1 × dpi / 72 と測定し、上流 `aa.prn`
  (36pt, ピッチ 74 行) でも同じ係数になることを確認して採用
  (PATCH_NOTES.md 8 節)。`cargo test --release` **69 passed**。
- 解像度: mm 換算を 208 dpi 固定から機種の dpi (270 / 360) に変更。
  上記「数値検証」のラベル長 (630 行 ≒ 76.9mm 等) は旧換算の値。

## UI モダン化 (2026-11-15, modern-web-guidance スキル適用)

- 検索クエリ: "accessible form labels" / "dark mode color-scheme" /
  "status message aria-live" / "responsive layout design" /
  "visible focus indicator" / "confirmation modal dialog"
- retrieve したガイド: forms, dark-mode, accessibility, css,
  responsive-design, dialog
- 主な変更 (server.py 内の HTML/CSS/JS のみ、単一ファイル維持):
  - CSS 変数 (2層トークン) + `color-scheme: light dark` と
    `light-dark()` でダークモード追随 (非対応ブラウザ向けに
    `@supports not` + `prefers-color-scheme` フォールバック併記)。
    `accent-color` / `scrollbar-color` もトークン化。
  - 各セクションをカード化、全コントロールに label (for/id) と
    fieldset/legend (データソース / 組み合わせ) を適用。
  - ステータス表示に role="status"、処理中メッセージは live region に
    流さずボタン無効化で代替。`:focus-visible` のアウトライン追加。
  - テンプレート削除をネイティブ `<dialog>` (showModal) の確認に変更。
  - ラベル一覧の項目を div から button に変更 (キーボード操作可)。
  - 見出しに clamp()、粗いポインタでタップ目標 48px。
- 再確認 (DRY_RUN + TEPRA_FAKE_TAPE=18 で再起動して実施):
  - GET / → 200、/api/tape → モック 18mm 応答。
  - プレビュー生成 OK (SVG に描画パス含む)。比率 7:3 のプレビュー
    SVG は 14,474 bytes で従来検証値と一致。
  - 差し込み zip = 3件 (`aa 1` `bb 2` `cc 3`)、cross = 9件 +
    マトリクス生成、CSV = 行ごと生成、いずれも従来通り。
  - 差し込みプレビュー / 連続印刷 (DRY_RUN) OK (done=3)。
  - テンプレート保存→取得の往復 (比率含む) OK、削除 OK。
  - ブラウザ実動: 削除ダイアログを開いて「削除する」→
    ステータスに削除完了が表示され、一覧から消えることを確認。
  - スクリーンショット: ui-review/09-modern-light.png (ライト)、
    10-modern-dark.png (ダーク、colorScheme エミュレーション)、
    11-modern-dialog.png (削除確認ダイアログ)。

## 改善一式: 履歴 / 文字コード / 進捗 / 実寸 / capture-kit / 書き出し・読み込み / 枠線 (2026-11-15 追加分)

### ドライバ (`--frame` / `--frame-width`)

- `cargo test`: **56 passed, 0 failed** (従来 52 + 枠線 4)。
  `cargo build --release` 成功。
- 枠線の数値確認 (18mm, "HOST\n192.168.0.1", --dump で比較):
  ジョブ長は枠の有無で不変 (20,497 バイト / 630 行)、インクドット数は
  15,891 → **18,695 (+2,804)**。インクのビット範囲は 14..166 →
  8..173 (上下に 8 ドットの余白 + 太さ、印刷可能帯でクランプ)。
- 枠なしの描画は従来と同一 (比率 7:3 のプレビュー SVG が従来検証値
  14,474 バイトと一致することで確認)。

### Web アプリ (API テスト 43 項目、全パス)

- プレビュー実寸: 比率 7:3 のプレビュー応答に tape_mm=18 /
  feed_rows=464 / **length_mm=56.7** が入り、UI に「実寸: 約 56.7mm ×
  18mm」と mm ルーラー (表示幅連動) を表示。
- CSV 文字コード: Shift_JIS の CSV (ヘッダ `名前,番号`、値 山田/佐藤)
  をファイル相当 (base64) で投入 → 自動判定が **shift_jis** を返し、
  テンプレート `{{名前}} {{番号}}` から **「山田 1」「佐藤 2」** の
  2 件を正しく生成。EUC-JP は手動指定でデコード成功、UTF-8 BOM は
  utf-8-sig と判定。貼り付け (csv_b64 経由) でも同じ経路。
- 印刷履歴: 単発印刷 (成功) が履歴に記録され、再印刷で新しい履歴が
  増えることを確認。履歴クリアで 0 件。上限は直近 100 件。
- 連続印刷の進捗/キャンセル: ジョブ化し job_id を即時返却。
  80 件のジョブを途中でキャンセル → state=cancelled、
  **done=6 (< 80)** で停止し、履歴に「キャンセル (6/80)」として記録。
  ブラウザ実動でも 200 件中 6 件でキャンセル停止を確認
  (ui-review/13-progress.png は印刷中 3/200 件の表示)。
- テンプレート書き出し/読み込み: export JSON に保存済みテンプレートが
  含まれ、import の rename で「T1 (2)」が作られ、overwrite でも
  取り込めることを確認。
- 回帰: 比率 7:3 プレビュー 14,474 バイト一致、差し込み 対応付け 3 件 /
  組み合わせ 9 件 + マトリクス、テンプレート往復 (枠線設定含む)、
  /api/tape モック、削除ダイアログの HTML 存在、ダークモード表示
  (履歴カードを colorScheme=dark で撮影し崩れなし) を再確認。

### capture-kit

- `analyze_captures.py` を自前ダンプのサンプル (6/12/18/24mm) で実行:
  構造表 (幅バイト/補助バイト/行数/ビットマップ長) が正しく出て、
  幅ペアの差分として既知の offset 8 (幅) / 23 (補助) / 2177
  (ヘッダチェックサム) のみが挙がることを確認。
- スクリーンショット: ui-review/12-history.png (履歴一覧)、
  13-progress.png (進捗)、14-frame.png (枠線 + ルーラー)、
  15-tools.png (書き出し/読み込み・文字コード選択)。

### 見送ったもの

- 行バンド境界への区切り線: バンド境界はレンダリング内部の情報で、
  後処理からは正確な位置が分からないため見送り (枠線のみ実装)。
- 認証: ユーザー方針 (リバースプロキシで実施) により実装しない。
