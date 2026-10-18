# 巻ごとの著者・役割をKomgaへ反映する

0.1.0rc1では、確認した著者と役割をKomga 1.29.0 / 1.29.1の巻メタデータへ反映・復元できる。
書庫や既読位置は変更しない。Webの著者編集はローカル表示用であり、このCLIとは別の操作になる。

## 対象と事前条件

[Komga作品情報ガイド](KOMGA_METADATA.md#事前設定) と同じ実行設定・管理者APIキー・パス対応を使う。
作品全体の書庫、ハッシュ、Komgaの作品/巻IDが一意に対応し、解析とハッシュ計算が完了していることが必要。
反映は1作品・1計画50巻まで。対象巻を指定する場合も作品全体の対応を検証する。
操作中はKomgaの対象巻の同時編集・自動取り込みを停止する。

## 名前と役割を確認する

```bash
manga-kit --config /path/to/execute.yaml scan
manga-kit --config /path/to/execute.yaml author-candidates --series LOCAL_SERIES_ID
```

候補の生成はローカル処理で、外部検索も書き込みもしない。
原作/作画等の表記、`×`、`&`、`/`、読点等から確認用の候補を作る。
全角の `ｘ`（`架空一郎ｘ架空二郎`、`ONEｘ架空`）は日本語名に隣接する場合に区切りとして扱う。
全角英字の単語の先頭・末尾や空白の前後でも、人名の文字は保持する。
英字だけを曖昧につないだ表記は自動分割しない。必要なら `×` または空白付きの `x` で区切る。
半角の `x` は、前後に空白がある場合と日本語の名前同士をつなぐ場合だけ区切る。
空白の後に「漫画:」「作画:」などの役割表記が続く場合は、そこから次の名前とする。
人名の中点・空白や `Alex`・`Max` 内の `x` は区切らない。「他」「不明」等を採用しない。
全作品へまとめて反映する場合は `prepare-komga-batch --mode authors --all` を使う。各作品のローカルの著者表記から上記の候補を作り、
全巻の既存一覧へ追加する計画を作る。出力の `targets[].authors` と `changes` で名前・役割を確認してから実行する。
著者が未設定・判定できない作品、著者がロック済みの巻がある作品は対象外として理由を出す。照合方法と復元は [一括反映](KOMGA_METADATA.md#複数作品をまとめて反映する) を参照する。
括弧付き所属の除去などは候補作成だけで行う。人名や括弧の意味を完全に判定できないため、必ず確認して明示指定する。

よく使う役割は `writer`（著者・原作）、`penciller`（作画）、`inker`、`colorist`、
`letterer`、`cover`、`editor`、`translator`。役割名は小文字へ正規化する。
同じ名前でも役割が違えば別の項目になる。明示指定した名前の中点・括弧・空白は保持する。

```bash
manga-kit --config /path/to/execute.yaml prepare-authors \
  --series LOCAL_SERIES_ID \
  --author writer '架空一郎' --author penciller '架空・二郎'
```

省略時は作品全巻が対象。巻ごとに著者が違う場合は `--book '作品フォルダ/第01巻.cbz'` を複数指定し、
同じ著者一覧を反映する巻を選ぶ。対象の違う一覧は計画を分け、順に反映・確認する。

既定では巻ごとの既存一覧・順番・役割を保ち、同じ名前/役割の組を重ねず追加する。
既存一覧を置き換える場合は `--replace-lists`、ロック済みの値を変更する場合は `--replace-locked` を追加する。
空一覧へ戻す場合は `--author` の代わりに `--clear-authors --replace-lists` を使う。
変更した巻の `authorsLock` は有効になる。元の値とロックは計画へ保存する。
1巻の一覧は反映後も100項目まで。正確に復元できない形式の既存値は上書きせず拒否する。

## 確認・実行・復元

`plan.books` の各巻の相対パス、ID、`before` / `after` の著者一覧とロックを確認する。
`plan.id` と `digest` を指定して実行する。

```bash
manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
manga-kit --config /path/to/execute.yaml operation OPERATION_ID
manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

実行前に全対象を検査し、各巻の送信意図をディスクへ確定してからPATCHする。
送信直前と送信後にも対象の対応・値・ロックを照合する。途中失敗・タイムアウト・強制終了後は再送せず、復元を使う。
復元は巻の逆順で、記録の変更前/変更後と現在値が一致するときだけ戻す。
通常の記録で未送信と確認できる巻は変更しない。既に元の値ならPATCHを省略する。
対象巻の著者やロックが後から別の値に変わっていれば、全対象の検査で中止する。

`plan.json`、`manifest.json`、`manifest.previous.json` と状態DBを一緒に保持する。
現行記録の欠落時は直前の記録から現在値を照合して復元できるが、実行には使えない。
復元後の反映は新しい計画で行う。Web履歴にも巻パス・著者・役割・ロックの差分を表示する。

KomgaのPATCHに条件付き更新がないため、最後の照合とPATCHの間の他クライアントの編集は防げない。
他者が偶然同じ値へ編集した場合も区別できない。Komgaの管理日時は巻き戻さない。
移動も行う場合は移動後のIDで計画し、戻す場合は著者情報を先に復元する。
物理的な保存先をバックアップから作り直すと旧計画は実行・復元できない。[更新・復旧手順](OPERATIONS.md) を参照する。

## 検証

架空データの単体テストで候補生成、複数役割、選択巻、一覧/ロックの扱い、衝突、複数巻の途中失敗、
通信結果不明、各PATCH前後のSIGKILL、復元途中のSIGKILLを検証した。
専用Komga 1.29.0でも2巻目の応答喪失からの復元、対象外の情報・元書庫・複数ユーザーの最新既読位置を確認した。
結果は [検証記録](VALIDATION.md) に記載する。

APIの根拠は公式の [BookMetadataUpdateDto](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/dto/BookMetadataUpdateDto.kt)、
[BookMetadata](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/domain/model/BookMetadata.kt)、
[Author](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/domain/model/Author.kt)。
