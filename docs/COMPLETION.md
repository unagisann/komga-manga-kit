# 完結情報をKomgaへ反映・復元する

0.1.0.dev8では、このツールで手動指定した完結状態を、CLIからKomga 1.29.0のタグ・連載状態へ反映できる。
既存KomgaToolの「他の分類タグを残す」「完結タグを重複させない」「休載・打ち切りの状態を保つ」処理を引き継ぐ。
完結の自動判定は行わない。Webで状態を保存しただけではKomgaへ反映されない。

## 変更する項目

| 項目 | 動作 |
| --- | --- |
| 連載中タグ | 指定したタグだけを取り除く。既定は `連載中` |
| 完結タグ | 指定したタグを追加する。既定は `完結`。既にあれば重複させない |
| その他のタグ | 保持する。このツールのローカルタグ一覧で置き換えない |
| タグのロック | 反映後は有効にする |
| Komgaの `ONGOING` | `ENDED` に変更し、連載状態のロックを有効にする |
| `ENDED` / `HIATUS` / `ABANDONED` | 状態とそのロックを保持する |
| 作品名・読み・出版社・ジャンル・その他の情報 | 変更しない |
| 書庫・ローカル編集・既読情報 | 変更しない |

ロック済みの値を変更する計画には `--replace-locked` が必要。
空・不明なKomgaの連載状態には反映しない。タグ名は1〜100文字で、連載中と完結に同じ名前は指定できない。
英字タグはKomgaの小文字化を変更後の値へ反映する。大文字小文字だけが異なる完結タグは追加しない。
指定した連載中タグ以外の分類を削除する機能や、連載中へ自動で戻す機能は含めない。

## 設定と計画

[Komga作品情報反映](KOMGA_METADATA.md#事前設定) と同じ接続・ハッシュ・パス対応・実行設定を使う。
POSIX環境、`app.demo: false`、`execution.enabled: true`、Komga 1.29.0 / 1.29.1が対象。
対象のローカル作品は `status: completed` に保存済みであること。

Webで状態を完結に保存してから、CLIの `scan` で作品IDを確認する。
Komga側の作品IDも確認し、両方を指定する。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml scan
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-completion --series LOCAL_SERIES_ID
# 完結にした全作品をまとめる場合
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-komga-batch --mode completion --all
```

タグ名を変える場合は `--ongoing-tag '連載中' --completed-tag '完結済み'` のように指定する。
ロック済みの値を変更する必要がある場合だけ、`--replace-locked` を付けて計画し直す。

計画はローカル作品・Komga作品・書籍構成・ハッシュを照合し、元の値・ロック、タグ名の指定、変更後を保存する。
`plan.changes`、`plan.before`、`plan.after` を確認する。計画の `kind` は `komga_metadata`、`mode` は `completion`。
計画だけではKomgaへ書き込まない。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
```

変更の意図と元の値を保存確定してから、対象作品へ1回だけPATCHする。
計画後のローカル状態・書庫・Komgaの対象項目・ロック・書籍IDの変化を実行前に検出する。
処理履歴にはタグ・連載状態・ロックの差分を表示する。

## 復元と操作の順番

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

元のタグ一覧・連載状態・ロックへ戻す。後から編集された対象項目は上書きしない。
同期後に進んだ既読位置、対象外のKomga情報、後からのローカル編集は保持する。
通信結果が不明な場合や強制終了後は自動再送せず、保存した操作と現在値を照合して復元する。
対象項目の一部だけが想定外の値になっている場合も、復元で無条件に上書きしない。

完結フォルダへの移動も行う場合は、**移動 → 移動後の作品ID確認 → 完結情報の同期** の順に進める。
両方を戻す場合は、**完結情報の復元 → 移動の復元** の順にする。
移動・再スキャンでKomgaの作品・書籍IDが変わるため、同期計画の後で書庫を動かすと、その計画からの反映・復元は拒否される。
元のパスへ戻した場合もIDの一致は保証しない。移動前に、既存の作品情報の操作を復元する必要がないか確認する。

通常の作品情報反映と同じ操作記録・排他制御・衝突を検出する復元を使用する。
APIキーは保存しない。Komga側の同時編集と自動取り込みは停止する。
Webの完結移動・情報反映を追加した。段階ごとに差分を確認する。MangaUpdatesの完結情報を候補として使えるが、無確認の完結判定・移動は行わない。[操作ガイド](TOOLS.md) を参照。

## 検証

架空データで既存タグ・状態・ロックの保持、実行条件、差分、衝突、通信結果不明、強制終了からの復元を確認する。
専用Komgaで移動 → 完結情報の同期 → 情報の復元 → 移動の復元と、複数ユーザーの最新既読位置を検証する。
結果は [検証記録](VALIDATION.md) と [既存ツールとの比較](COMPATIBILITY.md) を参照する。
連載状態・タグのAPI仕様は [公式モデル](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/domain/model/SeriesMetadata.kt) と
[更新DTO](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/dto/SeriesMetadataUpdateDto.kt) を確認した。
