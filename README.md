# Komga Manga Kit

日本語漫画の蔵書整理と、Komga連携のためのセルフホスト用OSS基盤です。
紙色・クレイ色のUI、五十音の絞り込み、自由に設定できるフォルダ・命名ルールを備えています。

**現在: v0.1の基盤（0.1.0.dev0）。** 本番向けのv0.1リリースではありません。
デモは外部サービスやAPIキーなしで起動します。漫画のダウンロード機能は含みません。

[English README](README.en.md) · [ロードマップ](docs/ROADMAP.md) · [構成・拡張](docs/ARCHITECTURE.md) · [設定](docs/CONFIGURATION.md)

## いま使える機能

- ホーム・蔵書一覧・作品名/著者/読みの検索・五十音フィルター
- ローカル書庫の再帰スキャン（CBZ / ZIP / CBR / RAR / 7z / EPUBの所在・巻数）
- CBZ / ZIPのComicInfo.xml、作品フォルダのseries.jsonの読み取り
- 半角/全角カナの正規化と、読みからの五十音分類
- 状態フォルダ名、作品フォルダ名、巻名、状態別階層の設定
- 整理プレビュー、巻数不明・整理先重複・既存ファイルの衝突検出
- Komga APIキー接続の確認・ライブラリ一覧・作品一覧API
- ライト/ダーク表示、レスポンシブUI、架空サンプルCBZの生成
- Docker Compose、CLI、基本テスト、機能/メタデータ取得元の拡張口

整理は**プレビュー専用**です。既存ファイルの移動・削除・書庫変換やKomgaへの書き込みは行いません。
外部からのメタデータ補完、読み生成、新刊チェック、完結移動、ポータル機能は今後追加します。
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
```

APIキーをローカルの環境変数へ設定して、`manga-kit serve` を起動します。
接続確認は設定画面から明示的に行います。起動だけではKomgaへ通信しません。
`config.yaml` の変更は再起動で反映されます。
UIで試した整理ルールはプレビュー用で、設定ファイルへ自動保存しません。

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
.venv/bin/python -m unittest discover -s tests -v
```

`scan` と `plan` はJSONを出力します。デモを無効にすると指定したローカルフォルダを読み取ります。
UIの整理プレビューは先頭200件を表示します。全件は `plan` で確認できます。

## Komga互換性

接続部分は `X-API-Key`、`GET /api/v1/libraries`、`GET /api/v1/series` を使用します。
実装時に[Komga公式OpenAPI](https://github.com/gotson/komga/blob/master/komga/docs/openapi.json)を確認し、
テストでは代表的な応答を模擬しています。**実機で検証済みのKomgaバージョンはまだありません。**
バージョン別の互換性保証は、v1.0までに実機で検証して公開します。
KomgaのDBへ直接アクセスしません。

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
