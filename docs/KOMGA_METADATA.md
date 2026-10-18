# Komgaへ作品情報を反映・復元する

0.1.0.dev7は、ローカルで確認・保存した作品情報を、明示的に有効にしたCLIからKomga 1.29.0へ反映する。
1回の計画で1作品・選んだ項目だけを扱う。Webでの情報保存や補完候補の採用だけではKomgaを変更しない。

## 対象項目

| このツールの項目（`--field`） | Komgaの項目 | 反映方法 |
| --- | --- | --- |
| `title` | `title` | 作品名を置き換える |
| `reading` | `titleSort` | 読みを並び順に使う。未設定の読みは反映できない |
| `publisher` | `publisher` | 出版社を置き換える |
| `summary` | `summary` | あらすじを置き換える |
| `genres` | `genres` | 既存のジャンルへ追加する |
| `tags` | `tags` | 既存のタグへ追加する |

反映する項目はKomga側でロックする。再取り込みによる上書きを防ぐため、ロックだけが変わる場合も差分へ表示する。
既にロックされている値を変える計画には `--replace-locked` が必要。
タグ・ジャンルの一覧を置き換える場合は `--replace-lists` も指定する。空の一覧による削除もこの指定が必要。

Komgaはジャンル・タグを小文字へ正規化する。英字の `Fantasy` は計画の変更後に `fantasy` と表示する。
このツールに保存した表記は変えない。Komga側の一覧は集合として比較し、順番だけの違いは変更として扱わない。

著者・役割は別の巻単位の操作 [著者反映ガイド](AUTHORS.md) で扱う。その他の巻情報、リンク、共有ラベル、年齢制限は対象外。
連載状態は `prepare-metadata` の対象外。0.1.0.dev8の専用操作 [完結情報の反映](COMPLETION.md) でタグと一緒に計画する。
書庫、series.json、Komgaの書籍・作品ID、既読位置は変更しない。スキャンや再解析も要求しない。
このツールの編集・外部情報源の対応付けは保持し、操作時の情報源を計画へ記録する。

## 事前設定

- POSIX環境、`app.demo: false`、`execution.enabled: true`。
- ライブラリと独立した、書き込み可能な `data.root`。漫画ライブラリは読み取り専用でもよい。
- Komga 1.29.0 / 1.29.1のURL、管理者APIキーの環境変数、ライブラリのルートに一致するパス対応。
- 対象書籍は解析とハッシュ計算を完了し、現在のローカル書庫と一致していること。
- Komgaは起動時・定期スキャン無効、ゴミ箱自動削除無効、自動変換・拡張子修復無効。

作品照合には [整理実行ガイド](ORGANIZATION.md#komga連携の条件) と同じハッシュ・一意性の検証を使う。
異なる内容、同じ内容の複数書籍やゴミ箱の候補、作品の一部だけの対応、未知のバージョンでは中止する。
ローカル作品IDとKomga作品IDを両方指定し、パスと書籍構成でも対応を検証する。作品名の一致だけでは反映しない。

```yaml
app:
  demo: false
library:
  root: /path/to/your/manga
data:
  root: /path/to/your/manga-kit-state
execution:
  enabled: true
komga:
  base_url: http://127.0.0.1:25600
  api_key_env: KOMGA_API_KEY
  path_mappings:
    - komga: /data/manga
      local: /path/to/your/manga
```

APIキーはYAMLやコマンド引数へ書かず、ローカルの環境変数へ設定する。
起動だけではKomgaへ通信しない。計画作成では読み取りと書籍一覧の問い合わせだけを行い、メタデータは変更しない。

## 計画・確認・反映

Webで手動編集・外部補完を保存してから、CLIの `scan` でローカル作品IDを確認する。
設定画面の「対応する作品を確認」でもKomga作品IDとの対応を確認できる。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml scan
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-metadata \
  --series LOCAL_SERIES_ID --field reading --field publisher --field genres --field tags
```

`--komga-series` は省略できる。省略時は、書庫のパスと内容ハッシュが一致するKomgaの巻から作品を1件に特定する。
指定した場合は、その作品と一致することを確認する。1作品の計画ではKomgaの書籍一覧とゴミ箱の一覧を1回ずつ取得する。

`plan.series_title`、`plan.series_path`、`plan.komga.series_id` と `plan.changes` を確認する。
変更前後の値とロック、タグ・ジャンルの追加/置き換えの指定を含めて確認する。
計画の `plan.id` と `digest` を使って実行する。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
```

計画後のローカル編集・書庫/series.json・Komgaの対象項目/ロック・書籍ID・パス対応の変化は実行前に拒否する。
変更前後の値と操作の意図をディスクへ確定してから、作品のメタデータへ1回だけPATCHする。
実行後にID・対応と、選んだ値・ロックの一致を読み取りで確認する。結果と差分はWebの処理履歴にも表示する。
選ばなかったメタデータと、その間に進んだ既読位置は書き込まない。

## 複数作品をまとめて反映する

全作品や複数作品は `prepare-komga-batch` で1つの計画にする。KomgaのIDは指定しない。
`--mode` は `metadata`（作品情報）・`completion`（完結情報）・`authors`（巻の著者）から選ぶ。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-komga-batch --mode metadata --all \
  --field reading --field publisher --field summary --field genres --field tags > batch-plan.json
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

出力の `targets[].changes` に作品ごとの変更前後とロック、`skipped` に対象外の作品と理由が入る。
`counts` は対象・対象外・変更なしの件数。全件の確認後に `operation_id` と `digest` で実行する。
`--all` の代わりに `--series` を繰り返して作品を選べる。1計画は5,000作品・50,000巻まで。

- **既定は空欄だけを埋める。** Komgaに値がある作品名・並び順・出版社・あらすじは保持する。並び順が作品名と同じ場合は空欄とみなす。
  既存の値も置き換える場合は `--overwrite` を指定する。タグ・ジャンルは1作品の計画と同じく追加方式。
- ローカルの値が空の項目は送らない。Komgaの値を消す場合は1作品の `prepare-metadata` を使う。
- ロック済みの項目に変更がある作品は対象外になる。含める場合は `--replace-locked` を指定する。
- `completion` はこのツールで完結にした作品だけを対象にする。タグ・状態の扱いは [完結情報](COMPLETION.md) と同じ。
- `authors` はローカルの著者表記から作った候補（[著者の候補](AUTHORS.md)）を全巻へ追加する。計画で名前と役割を確認する。

### 一括反映での照合

一括反映は書庫の内容を読まない。計画時に各巻のサイズ・inode・更新日時・変更日時を記録し、実行・復元の前に一致を確認する。
Komga側は、パス対応・作品の巻構成・巻のサイズ・解析済みハッシュ・書籍IDで対応付け、計画時の値と比べる。
Komgaの書籍一覧と作品一覧は、計画時と実行の前後に各1回取得する。1.4TB規模でも書庫を読み直さない。
サイズと日時を保ったまま中身だけを差し替えた書庫は検出できない。書き込み先はKomgaの作品・巻の情報だけで、書庫は変更しない。
厳密に照合する場合は、内容ハッシュを確認する1作品の計画を使う。

作品ごとに送信予定をディスクへ記録してから、直前の値を読み直してPATCHする。途中で失敗しても再送はしない。
`restore` は送信予定を記録した作品だけを逆順に戻す。反映後に編集された作品があれば、上書きせず全体を中止する。
現行の記録が壊れた場合は直前の記録から復元し、全作品を送信済みの可能性があるものとして扱う。

## Komgaの並び順から読みを取り込む

既存のKomga運用で並び順（titleSort）に読みを入れている場合、このツールの読みとして取り込める。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-reading-import --all > reading-plan.json
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

並び順の先頭のかなを読みとする。「カナ 題名」の形式（既存KomgaToolの形式）と、かなだけの並び順に対応する。
並び順が作品名と同じ作品（Komgaの既定値）と、かなで始まらない作品は取り込まない。
読みが設定済みの作品（手動編集・series.json・補完）は上書きしない。計画後に読みが設定された場合は実行を中止する。
取り込みはローカルの編集として1回のトランザクションで保存し、情報源にKomgaと作品IDを記録する。Komgaと書庫は変更しない。
`restore` は取り込み後に編集されていなければ、全作品を一度に元へ戻す。

## 復元・中断からの回復

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

完了・失敗・実行中・復元中の操作に使える。元の値と元のロック状態へ戻す。
対象項目が計画の反映後と一致するときだけ復元する。既に元の値なら送信せず復元済みとして記録する。
反映前/反映後のどちらにも一致しない対象項目や、別の書籍ID・パス対応は上書きしない。
対象外のKomga項目や、反映後に保存したローカル編集・既読位置は保持する。

タイムアウトや強制終了後は、同じ計画を再実行しない。送信済みか不明でも、操作記録と現在の値を照合して復元する。
現行の記録が欠落・破損した場合は直前の記録から復元できる。直前の記録を使った再実行は拒否する。
復元後に再び反映する場合は、新しい計画を作成して確認する。

計画と記録は `data.root/operations/<library-namespace>/<operation-id>/` に保存する。
`plan.json` が変更前情報のバックアップで、`manifest.json` と `manifest.previous.json` が進捗を持つ。
APIキーは保存しない。復元が不要になるまで、このフォルダと状態DBを一緒に保持する。
整理・再スキャンでKomgaのIDが変わると、この操作からの復元は拒否する。元のパスへ戻してもIDの一致は保証しない。
移動も必要な場合は、移動後に新しい作品IDを確認して情報反映を計画し、戻すときは情報反映を先に復元する。

KomgaのPATCHには条件付き更新による排他機能がないため、最終確認からPATCHまでの他クライアントの編集は防げない。
計画から反映・復元の間はKomga側の対象作品を別の画面やツールから編集せず、自動取り込みも停止する。
同じ値へ変更した第三者の編集と、この操作の結果は区別できない。Komgaの管理日時は元には戻さない。

## 検証とAPIの根拠

[公式OpenAPI](https://github.com/gotson/komga/blob/1.29.0/komga/docs/openapi.json)、
[作品メタデータのPATCH処理](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/SeriesController.kt)、
[保存時の正規化](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/domain/model/SeriesMetadata.kt)を確認した。
隔離したKomga 1.29.0で6項目の反映・ロック・復元、後からの編集の衝突、2ユーザーの未読・途中・読了と最新の既読位置を検証する。
バージョン別の互換性は未検証。通常のテストは外部接続せず、実機テストは [専用サーバーでの実行手順](ORGANIZATION.md#実機テストを再実行する) を使う。
