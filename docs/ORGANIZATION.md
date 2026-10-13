# 整理の実行と復元

0.1.0.dev2以降では、確認済みの命名・配置計画をCLIから実行する。
0.1.0.dev4でバックアップ・移動先・操作記録の保存確定を補強した。
Web画面と既定設定はプレビュー専用。初回は架空サンプルなどのテスト用ライブラリで確認する。

## 実行環境

- POSIX環境（Linuxなど）、同一ファイルシステム内の作品単位の移動。
- ライブラリに読み書きできるユーザーと、別の `data.root`。
- 全対象の書庫と `series.json` をコピーできるバックアップ容量。
- 実行・復元中は他のツールによる書庫変更、Komgaの手動スキャン、読書操作を停止する。

```yaml
app:
  demo: false
library:
  root: /path/to/test-library
data:
  root: /path/to/manga-kit-state
execution:
  enabled: true
```

整理ルールは `layout` に保存する。Webで試したルールは自動保存されない。
完結への移動は作品情報の `status: completed` と `layout.completed` に従う。完結の自動判定は未実装。
通常のComposeはライブラリを読み取り専用でマウントするため、ホストの専用CLI設定などを使う。
ホストとコンテナでルートパスが異なると編集・履歴の名前空間も異なる。既存の編集を使う場合は、
同じルートパスと同じ実行データを参照する環境で実行する。

## 確認して実行する

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml scan
.venv/bin/manga-kit --config /path/to/execute.yaml plan
.venv/bin/manga-kit --config /path/to/execute.yaml prepare --series SERIES_ID
```

`SERIES_ID` は `scan` の作品ID。複数作品は `--series` を繰り返す。省略すると全作品が対象になり、
変更のない書庫もバックアップ・検証に含む。巻数不明・衝突・スキャン上限などの要確認項目がある場合は計画を保存しない。

`prepare` はライブラリを変更せず、次の内容をJSONで返す。

| 項目 | 確認内容 |
| --- | --- |
| `plan.entries` | 元と先の相対パス、書庫/series.jsonの種別、サイズ・ハッシュなど |
| `plan.groups` | 作品フォルダとローカル編集の移動先 |
| `plan.komga` | Komgaの対応、既読位置、ロックした情報（連携時） |
| `plan.id` | 実行・復元に使う操作ID |
| `digest` | この計画の確認値 |

対象と変更先を確認してから実行する。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
.venv/bin/manga-kit --config /path/to/execute.yaml jobs
```

計画作成後のファイル変更・差し替え・編集・Komga既読情報の変化を再確認し、全対象をバックアップする。
バックアップの内容と保存先ディレクトリを同期してからもう一度確認し、上書きしないハードリンクを作成する。
移動先の内容・名前と新しい親フォルダを同期してから元の名前を削除し、元の親フォルダも同期する。
書庫の内容、ファイルのモード、更新日時を保持する。完了した移動はこのツールの編集も新しい作品IDへ対応付ける。
同じライブラリの実行・復元を排他制御し、その間のWebからのメタデータ保存は409で拒否する。
処理状況はCLIの `operation` とWebの処理履歴で確認できる。

対象はスキャンした書庫と `series.json`。他の補助ファイルと空フォルダは元の場所に残す。
バックアップから作り直すファイルの所有者は実行ユーザーになる。所有者・ACL・拡張属性の復元は未対応。
既存の別作品への統合、作品の分割、シンボリックリンク、別ファイルシステムへの移動、元書庫を置き換える変換は未対応。
元書庫を保持するCBZ生成は、別の [CBZ生成ガイド](CONVERSION.md) を参照する。
大文字小文字・互換文字だけが異なる名前も衝突として扱う。

## 復元する

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

完了した操作、途中で失敗した操作、プロセスが停止した操作を復元できる。
全対象の元/先の状態とバックアップのハッシュを確認してから元の名前へ戻す。
元と先の両方からファイルがなくなった場合は、検証済みバックアップから復元する。
元の名前に別のファイルがある、移動先の書庫が変更された、ローカル編集が更新された、
バックアップが壊れた場合は、上書きせず中止する。状況を確認してから同じ操作IDで再実行する。

失敗時に自動復元は行わない。変更されたファイルを確認し、明示的に `restore` を実行する。
成功した整理の後に読書が進んでいる場合、復元直前の既読情報を確認して保持する。
`library.root`、`data.root`、Komga接続先・パス対応は復元時も同じものを使う。
ルートディレクトリの差し替えは拒否する。ルートを移設する前に、必要な復元を完了する。

実行データは次の構成で保持する。APIキーは保存しない。

```text
data.root/
  state.sqlite3                           編集・ジョブ・操作履歴
  operations/<library-namespace>/
    lock                                  同時操作を防ぐロック
    <operation-id>/
      plan.json                           変更前情報と確認済み計画
      manifest.json                       実行・復元の途中状態
      manifest.previous.json              直前に確定した途中状態
      backup/0, backup/1, ...              対象ごとの元ファイル
```

計画・バックアップ・DBは一緒に保持する。操作ごとのバックアップ削除機能は未実装。
復元不要と判断するまでは、アプリを停止して実行データ全体をバックアップする。
停止後の記録の読み取りと復元用一時ファイルの扱いは [保存と中断からの復元](DURABILITY.md) を参照する。

## Komga連携の条件

実行と復元の連携は、隔離した実機で検証した **Komga 1.29.0** に限定する。
`komga.base_url`、管理者APIキーの環境変数、ライブラリのルートに一致する `komga.path_mappings` が必要。
未設定のKomga連携ではローカルの移動のみ行う。Komga管理下の蔵書では連携を設定してから実行する。

| Komgaライブラリ設定 | 必須値 |
| --- | --- |
| `hashFiles` | `true`。全対象の解析とファイルハッシュ計算を完了する |
| `scanOnStartup` | `false` |
| `scanInterval` | `DISABLED` |
| `emptyTrashAfterScan` | `false` |
| `convertToCbz` / `repairExtensions` | 両方 `false` |

現在の書庫をKomga 1.29.0と同じXXH3-128（seed 0）で検証する。
ハッシュの不一致、サーバー全体での同じ内容の書籍、同じハッシュのゴミ箱候補がある場合は中止する。
対応が一意で、Komgaの作品全体を移動する場合だけ進む。
実行後にライブラリのスキャンをAPIで要求し、同じ内容に対するKomgaの既読・ロックした情報の復元を確認する。
実行時のAPIキー利用者の既読位置・読了・読書日時と、ロックした書籍/作品情報を実行後に照合する。
複数ユーザーの既読引き継ぎは、下記の実機テストで検証している。KomgaのDBを直接参照・変更しない。

Komgaの書籍/作品IDと既読レコードの作成日時などは変わる。IDに依存する外部連携の継続は未検証。
タグ等の未ロック情報はKomgaの取り込みルールに従う。全メタデータ、コレクション、読書リスト、サムネイルを
このツールがバックアップ・検証する機能は未実装。
動作の根拠は [Komga 1.29.0の内容一致による復元](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/domain/service/LibraryContentLifecycle.kt) と
[ハッシュ計算](https://github.com/gotson/komga/blob/1.29.0/komga/src/main/kotlin/org/gotson/komga/infrastructure/hash/Hasher.kt)。

## 実機テストを再実行する

通常の単体テストは外部サービスを使用しない。実機テストは `tests/komga_integration.py` に分離する。
専用のKomga 1.29.0をループバックだけに公開し、テスト用ホストディレクトリを `/verification` へ読み取り専用でマウントする。
専用サーバーの管理者APIキーを `KOMGA_TEST_API_KEY` に設定する。

```bash
export KOMGA_INTEGRATION_CONFIRM=ISOLATED_TEST_SERVER
export KOMGA_INTEGRATION_URL=http://127.0.0.1:25601
export KOMGA_INTEGRATION_DIRECTORY=/path/to/test-verification
.venv/bin/python -m unittest discover -s tests -p komga_integration.py -v
```

専用サーバー以外には実行しない。テストはUUID付きの一時フォルダ・ライブラリ・読書ユーザー2名だけを作成し、
終了時にそれらだけを削除する。衝突拒否、命名と完結フォルダ移動、未読/途中/読了と読書日時、移動後の最新既読位置、
ロックした作品情報、ローカル編集、移動先を削除した後のバックアップ復元、全ファイルのSHA-256一致を確認する。
