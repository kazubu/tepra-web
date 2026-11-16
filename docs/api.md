# HTTP API

`server.py` の画面が使っているのと同じ API です。curl やスクリプトから
そのまま叩けるので、IPAM や台帳スプレッドシートからラベルを出す用途に
使えます。

ベース URL は Web 画面と同じ (既定 `http://<ホスト>:8090`)。
リクエストとレスポンスはどちらも JSON (UTF-8)、`Content-Type:
application/json` を付けてください。

## 先に知っておくこと

- **認証はありません。** 信頼できる LAN 内での利用が前提です。外に出す
  場合はリバースプロキシ側で認証してください (この方針は変えません)。
- **プリンタは 1 台・同時に 1 ジョブです。** 印刷・プレビュー・テープ送り
  はサーバ内のロックで直列化されます。並列にリクエストしても順番待ちに
  なるだけで、壊れはしません。
- **単発印刷は印刷が終わるまで返りません。** ドライバがプリンタの状態を
  見て、idle に戻ってから応答します。1 枚あたり 5 秒前後かかります。
- **差し込み印刷は非同期です。** `job_id` を返すので、状態は
  `/api/merge/status` で見ます。
- **失敗は `{"ok": false, "error": "..."}`** です。HTTP のステータスは
  基本的に 200 で、見つからないジョブなど一部だけ 404 を返します。
  `ok` を見てください。
- **プリンタが応答しない場合**もエラーとして返ります (ドライバの
  メッセージがそのまま `error` に入ります)。

## 印刷する

### POST /api/print

テキストを 1 枚印刷します。印刷が終わってから返ります。

```bash
curl -s http://printer.local:8090/api/print \
  -H 'Content-Type: application/json' \
  -d '{"opts": {"tape": "9", "size": "16", "cut": "half"}, "text": "SW-001\n10.0.0.1"}'
```

```json
{"ok": true, "message": "印刷しました。", "detail": "...ドライバの状態ログ..."}
```

`text` の改行はそのまま複数行ラベルになります。`{{today}}` と `{{now}}`
は印刷時の日時に置き換わります。

### POST /api/merge/print

複数ラベルを **1 ジョブ** として送ります。ラベル間にもカット設定が効く
ので、ハーフカットなら台紙がつながった 1 本のテープになります。
すぐに `job_id` を返します。

```bash
curl -s http://printer.local:8090/api/merge/print \
  -H 'Content-Type: application/json' \
  -d '{"opts": {"tape": "9", "cut": "half"}, "labels": ["SW-001", "SW-002", "SW-003"]}'
```

```json
{"ok": true, "job_id": "0139750b0aa1", "total": 3}
```

`labels` は文字列の配列です。どう作るかは自由で、`/api/labels` を使っても、
呼び出し側で組み立てても構いません。

### GET /api/merge/status?job=&lt;job_id&gt;

```json
{"ok": true, "state": "running", "done": 2, "total": 3,
 "phase": "3 件を 1 ジョブとして印刷中 (66% 送信)",
 "detail": null, "error": null, "aborted_mid_job": false, "abort_error": null}
```

`state` は `running` / `done` / `cancelled` / `error` のいずれかです。
`done` は送信済みバイト数から割り戻した概数です。プリンタは 44KB 以上を
一気に受け取るので、それより小さいジョブでは送信が一瞬で 100% になり、
印刷の進み具合とは一致しません (9mm で 10 枚程度、24mm なら 1〜2 枚が
目安)。それを超える長さのジョブでは、プリンタが印刷した分しか受け取らなく
なるため、実際の進み具合になります。

### POST /api/merge/cancel

```bash
curl -s http://printer.local:8090/api/merge/cancel \
  -H 'Content-Type: application/json' -d '{"job_id": "0139750b0aa1"}'
```

送信前なら 1 枚も印刷しません。送信中ならドライバを止めて残りを送らず、
プリンタにも中断 (SOFT_RESET) を指示します。プリンタは印刷中だったラベルを
最後まで出し、カット設定どおりに切ってから止まります。このときの状態は
`state: "cancelled"` かつ `aborted_mid_job: true` です。

実測 (SR750、9mm、10 枚、ハーフカット): ジョブは 0.24 秒で全部プリンタに
入るため、6 秒後にキャンセルしても「送信を止める」効果はなく、止めている
のは SOFT_RESET です。12.1 秒に停止しました (全部印刷すれば約 50 秒)。

## 確認する

### POST /api/preview

印刷せずに SVG を作ります。印刷と同じラスタから描くので、見た目は印刷結果と
一致します。

```bash
curl -s http://printer.local:8090/api/preview \
  -H 'Content-Type: application/json' \
  -d '{"opts": {"tape": "9"}, "text": "TEST"}'
```

```json
{"ok": true, "svg": "<svg ...>", "length_mm": 28.7, "tape_mm": 9,
 "feed_rows": 407, "dpi": 360.0, "notes": []}
```

ラベルの実寸を知りたいだけなら `length_mm` を見てください。

### GET /api/tape

装着されているテープ幅をプリンタに問い合わせます。

```json
{"ok": true, "tape_mm": 9, "index": 2, "supported": true, "mock": false}
```

### GET /api/config

サーバの設定 (機種、解像度、DRY_RUN かどうか) を返します。

## その他

| メソッド | パス | 用途 |
|---|---|---|
| POST | `/api/feed` | `{"mm": 10, "cut": "none"}` で空白テープを送る (`cut` は `none` / `full`) |
| POST | `/api/labels` | 差し込みのラベル文字列を作る (下記) |
| POST | `/api/merge/preview` | `{"opts", "labels", "index"}` で一覧の 1 件をプレビュー |
| GET | `/api/fonts` | `fonts/` にあるフォント一覧 |
| GET | `/api/templates` | 保存済みテンプレート |
| POST | `/api/templates` | `{"name", "template"}` で保存 |
| POST | `/api/templates/delete` | `{"name"}` で削除 |
| GET | `/api/history` | 直近 100 件の印刷履歴 |
| POST | `/api/history/clear` | 履歴の全削除 |

`/api/labels` は CSV か系列リストからラベル文字列を作ります。

```bash
curl -s http://printer.local:8090/api/labels \
  -H 'Content-Type: application/json' \
  -d '{"source": "csv", "template": "{{name}}\n{{ip}}", "csv": "name,ip\nSW-001,10.0.0.1\nSW-002,10.0.0.2"}'
```

```json
{"ok": true, "labels": ["SW-001\n10.0.0.1", "SW-002\n10.0.0.2"], "records": [...], "meta": {...}}
```

系列リストなら `{"source": "series", "template": "...", "series": {"A": ["1","2"]},
"mode": "zip"}` です (`mode` は `zip` = 対応付け、`product` = 組み合わせ)。

## opts の中身

印刷とプレビューで共通です。省略した項目はドライバの既定値になります。
値は文字列でも数値でも構いません。

| キー | 例 | 意味 |
|---|---|---|
| `tape` | `"9"` / `"auto"` | テープ幅 (mm)。`auto` はプリンタに問い合わせ |
| `size` | `"16"` | 文字サイズ (pt)。省略でテープ幅に収まる最大 |
| `font` | `"MSGothic.ttc"` | `fonts/` 内のファイル名 |
| `align` | `"left"` / `"center"` / `"right"` | 行の揃え |
| `length_mm` | `"30"` | ラベル長 (mm)。省略すると文字の長さで決まる |
| `margin_preset` | `"small"` | 余白プリセット (`none` / `xsmall` / `small` / `medium` / `large` / `custom`) |
| `left_margin_mm` / `right_margin_mm` | `"4"` / `"3"` | 送り方向の余白 (mm) |
| `cut` | `"full"` / `"half"` / `"none"` | カット方式 |
| `copies` | `"2"` | 同じラベルの枚数 (1 ジョブ内) |
| `ratios` | `"7,3"` | 行ごとの文字サイズ比率 |
| `frame` | `true` | 枠線を付ける |
| `frame_width` | `"2"` | 枠線の太さ (ドット、1〜3) |
| `frame_mm` | `"36"` | 枠線の横幅 (mm)。省略すると文字を囲む |
| `cable` | `true` | ケーブル表示ラベルの配置 |
| `cable_mm` | `"7.5"` | ケーブルラベルの印刷エリア (mm) |
| `vertical` | `"stacked"` / `"rotated"` | 縦書き。ラベル長・比率とは併用不可 |

## 使用例: 台帳からまとめて印刷する

```bash
#!/bin/sh
# 1 列目を機器名、2 列目を IP とする CSV からラベルを作って印刷する
HOST=http://printer.local:8090
CSV=$(cat devices.csv | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')

LABELS=$(curl -s $HOST/api/labels -H 'Content-Type: application/json' \
  -d "{\"source\": \"csv\", \"template\": \"{{name}}\\n{{ip}}\", \"csv\": $CSV}" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["labels"]))')

JOB=$(curl -s $HOST/api/merge/print -H 'Content-Type: application/json' \
  -d "{\"opts\": {\"tape\": \"9\", \"cut\": \"half\"}, \"labels\": $LABELS}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# 終わるまで待つ
while :; do
  STATE=$(curl -s "$HOST/api/merge/status?job=$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$STATE" = "running" ] || break
  sleep 2
done
echo "$STATE"
```

実機なしで試すときは `TEPRA_DRY_RUN=1` でサーバを起動してください。
印刷の代わりにジョブを生成するだけになり、テープ送りと装着テープの確認も
`TEPRA_DRY_TAPE_MM` の幅を仮定して動きます。
