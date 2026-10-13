# Komga Manga Kit

日本語漫画の蔵書整理と、Komga連携のためのセルフホスト用OSS基盤です。
紙色・クレイ色のUI、五十音の絞り込み、自由に設定できるフォルダ・命名ルールを備えています。

**現在: v0.1の第3段階（0.1.0.dev3）。** 本番向けのv0.1リリースではありません。
デモは外部サービスやAPIキーなしで起動します。漫画のダウンロード機能は含みません。

[English README](README.en.md) · [ロードマップ](docs/ROADMAP.md) · [構成・拡張](docs/ARCHITECTURE.md) · [設定](docs/CONFIGURATION.md)

## いま使える機能

- ホーム・蔵書一覧・作品名/著者/読みの検索・五十音フィルター
- ローカル書庫の再帰スキャン（CBZ / ZIP / CBR / RAR / 7z / EPUBの所在・巻数）
- CBZ / ZIPのComicInfo.xml、作品フォルダのseries.jsonの読み取り
- 半角/全角カナの正規化と、読みからの五十音分類
- 状態フォルダ名、作品フォルダ名、巻名、状態別階層の設定
- 整理プレビュー、巻数不明・整理先重複・既存ファイルの衝突検出
- CLIで確認した計画の命名・移動、全対象のバックアップ、上書きしない復元（明示的な有効化が必要）
- 完結フォルダへの配置と、検証済みKomga 1.29.0の既読位置・ロックした情報の引き継ぎ
- ZIP/CBZ・RAR/CBR・7zから別の保存先へCBZ生成、ページ名と格納順の正規化、ComicInfo.xmlの保持
- スキャン・整理プレビューの処理ジョブ、冊数の進捗、結果・操作履歴の保存
- 作品名・著者・読み・状態・あらすじ・出版社の手動編集、差分確認、編集の解除
- Komga APIキー接続の確認・ライブラリ一覧・作品一覧API
- Komgaとローカルのパス対応付け、対応する作品の確認
- ライト/ダーク表示、レスポンシブUI、架空サンプルCBZの生成
- Docker Compose、CLI、基本テスト、機能/メタデータ取得元の拡張口

既定の設定とWeb画面の整理は**プレビュー専用**です。CLIの実行は `execution.enabled: true` で有効にします。
実行はPOSIX・同一ファイルシステム内・作品単位に限り、全対象をバックアップしてから移動します。
移動のKomga連携時は明示的にスキャンを要求します。CBZ生成では元書庫を保持し、別の保存先へ出力します。
変換した書庫による元書庫の置き換え、変換先への既読引き継ぎ、外部補完情報の書き込みは未対応です。
手動編集はこのツールの実行データへ保存し、蔵書検索・五十音分類・整理プレビューに反映します。
外部からのメタデータ補完、AIによる読み生成、新刊チェック、完結判定の自動化、ポータル機能は今後追加します。
現段階の表紙は共通デザインで描画し、実際の書庫表紙の抽出は未対応です。

## 起動

Python 3.11以上。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/manga-kit serve
```

<http://127.0.0.1:5080> を開いてください。起動時の既定はサンプル蔵書です。
`serve` は開発用サーバーです。コンテナではGunicornを使います。

### Docker Compose

```bash
docker compose up --build -d
```

同じURLでサンプルUIが起動します。既存のKomgaを起動・変更するサービスは含んでいません。
ホスト側の公開先は既定でループバックの5080番です。
編集内容と処理履歴は、名前付きボリューム `manga-kit-state` の `/state` に保存します。
コンテナを再作成しても保持されます。`docker compose down -v` は保存用ボリュームも削除します。

実ライブラリの読み取りは、設定ファイルと漫画フォルダを別々にマウントします。

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

`config.yaml` の `app.demo` を `false`、`library.root` を `/library` に変更します。
`compose.local.example.yaml` を `compose.local.yaml` にコピーして、マウント元を自分の環境へ変更します。
実際の `compose.local.yaml` は公開用リポジトリへ追加しないでください。

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d
```

漫画フォルダは読み取り専用マウントです。Komgaが別のコンテナの場合、`localhost` はこのツール自身を指すため、
同じDockerネットワークのサービス名など、このツールから接続できるURLを指定します。

### 自分の蔵書を指定

```bash
cp config.example.yaml config.yaml
```

```yaml
app:
  name: 私の漫画棚
  demo: false
library:
  root: /path/to/your/manga
data:
  root: /path/to/your/manga-kit-state
layout:
  status_folders:
    ongoing: 連載中
    completed: 完結
    unknown: 未分類
  series: "[{author}] {title}"
  book: "{title} 第{volume:02}巻{extension}"
  ongoing: "{status}/{series}"
  completed: "{status}/{kana_row}/{series}"
  unknown: "{status}/{series}"
komga:
  base_url: http://127.0.0.1:25600
  api_key_env: KOMGA_API_KEY
  path_mappings:
    - komga: /data/manga
      local: /path/to/your/manga
```

APIキーをローカルの環境変数へ設定して、`manga-kit serve` を起動します。
接続確認は設定画面から明示的に行います。起動だけではKomgaへ通信しません。
`config.yaml` の変更は再起動で反映されます。
UIで試した整理ルールはプレビュー用で、設定ファイルへ自動保存しません。
`komga.path_mappings` はKomgaが見ている保存先と、このツールが見ている保存先の対応です。
設定画面の「対応する作品を確認」で明示的に照合します。Komgaやライブラリへ書き込みません。

### 作品情報と処理履歴

メタデータ画面で作品名を選び、編集 →「変更内容を確認」→「この内容で保存」と進みます。
読みは半角・全角カナをひらがなに揃えます。「読み取った情報に戻す」で編集を解除できます。
別の操作や書庫側で情報が変わった場合は、差分を再確認してから保存します。

処理履歴画面からスキャンと整理プレビューを実行できます。整理画面の更新結果も記録します。
実行データの既定は設定ファイル基準の `var/state` です。`data.root` または `MANGA_KIT_DATA_ROOT` で変更できます。
漫画ライブラリと重なる保存先は使えません。バックアップはアプリを停止して、このフォルダ全体を保存してください。

任意の設定ファイルを使う場合:

```bash
.venv/bin/manga-kit --config /path/to/config.yaml serve
```

### サンプルCBZでスキャンを試す

```bash
.venv/bin/manga-kit sample --output var/sample-library
cp config.example.yaml config.yaml
```

`config.yaml` の `app.demo` を `false` にして起動すると、生成したファイルを実際にスキャンします。
サンプルは架空の8作品・22冊、独自の簡易画像とComicInfo.xmlです。実在の漫画や蔵書情報は同梱していません。
生成先は空のフォルダに限ります。

### CLI・テスト

```bash
.venv/bin/manga-kit scan
.venv/bin/manga-kit plan
.venv/bin/manga-kit plan --record
.venv/bin/manga-kit jobs
.venv/bin/manga-kit job JOB_ID
.venv/bin/python -m unittest discover -s tests -v
```

`scan` と `plan` はJSONを出力します。デモを無効にすると指定したローカルフォルダを読み取ります。
UIの整理プレビューは先頭200件を表示します。全件は `plan` で確認できます。
`scan --record` / `plan --record` はジョブと結果を保存し、ジョブIDを含むJSONを返します。
保存した結果の全件は `job JOB_ID` で確認できます。CLIも保存済みの作品情報を使います。

### 整理を実行・復元する

まずテスト用の書庫で [整理実行ガイド](docs/ORGANIZATION.md) の設定・制約を確認してください。
`app.demo: false`、書き込み可能なライブラリ、別の保存先 `data.root`、`execution.enabled: true` が必要です。
通常のComposeのライブラリマウントは読み取り専用のため、CLIの移動はホスト側の専用設定などで行います。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml prepare --series SERIES_ID
# 出力の plan.entries と plan.groups を確認し、plan.id と digest を控えます。
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

途中で停止した場合も同じ操作IDから復元できます。移動後に変更されたファイルや復元先の衝突は上書きしません。
`data.root` の操作記録・バックアップは、復元が不要になるまで保持してください。

### CBZへ変換する

[CBZ生成ガイド](docs/CONVERSION.md) に対応範囲・ページ順・取り消しの手順を記載しています。
同じ `execution.enabled: true` の設定を使い、元ライブラリ・実行データと重ならない出力フォルダを事前に作成します。

```bash
mkdir -p /path/to/converted-library
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-conversion \
  --book '作品/作品 第1巻.zip' --output /path/to/converted-library
# plan.entries のページ対応を確認してから実行します。
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
# 取り消しは変更されていない生成CBZだけを削除します。元書庫は保持します。
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

画像を再エンコードせず、自然順で `0001.png` などへ揃えます。ComicInfo.xmlと補助ファイルの内容は保持します。
RAR/7zにはシステムのlibarchiveも必要です。Dockerイメージには同梱し、Debian系のホストでは
`sudo apt-get install libarchive-dev` で導入できます。ZIP/CBZはPython標準ライブラリで読み取ります。

## Komga互換性

接続は `X-API-Key` と [Komga 1.29.0公式OpenAPI](https://github.com/gotson/komga/blob/1.29.0/komga/docs/openapi.json) を使います。
隔離したARM64環境の **Komga 1.29.0** で、接続・作品照合・命名・完結フォルダへの移動・復元を検証しました。
複数ユーザーの未読・途中・読了、最新の既読位置、ロックした作品情報を確認しています。
移動時にはKomga側の書籍/作品IDや一部の管理日時が変わります。ID参照の外部連携は未検証です。

移動を連携して実行する場合は1.29.0に限定し、ハッシュ計算済み・自動スキャン無効・ゴミ箱自動削除無効・自動変換無効を要求します。
現在の書庫とKomgaのハッシュが異なる場合、同じ内容の書籍やゴミ箱の候補がある場合は中止します。
実行後にAPIで再スキャンし、Komgaの内容一致による復元と既読情報を確認します。DBへ直接アクセスしません。
検証の範囲と再実行手順は [検証記録](docs/VALIDATION.md) と [整理実行ガイド](docs/ORGANIZATION.md) を参照してください。
生成CBZは別のテストライブラリで4ページの順序と画像内容・作品情報を検証しています。
元ライブラリのバイト列・書籍ID・2ユーザーの既読状態を保持することも確認しました。

## 公開範囲

このフォルダは、既存運用のコード・秘密情報・状態データ・ログ・Git履歴をコピーせず作成しています。
設定と実行データはGit/Dockerビルド対象から除外し、ライブラリの位置はソースの配置から独立しています。
取得候補の検索・漫画ダウンロード・ダウンローダー連携は対象外です。
[GitHub](https://github.com/unagisann/komga-manga-kit)でソースコードを公開しています。

LAN等へ公開する場合は `MANGA_KIT_ADMIN_TOKEN` を環境変数で設定できます。
ユーザー名 `admin`、その値をパスワードとしてHTTP Basic認証を使用します。
通信の暗号化はHTTPS対応のリバースプロキシで行ってください。

## License

[MIT](LICENSE)。
