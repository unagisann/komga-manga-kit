# Komga Manga Kit

日本語漫画の蔵書整理と、Komga連携のためのセルフホスト用OSS基盤です。
紙色・クレイ色のUI、五十音の絞り込み、自由に設定できるフォルダ・命名ルールを備えています。

普段の操作は **作品フォルダへ漫画を追加 → ボタンで差分を確認 → ノーマライズを実行** です。
名前・配置の整理に、CBZ化と書庫内のページ順修正をまとめています。
起動直後は、蔵書を探して読むためのポータルを表示します。漫画を追加したときは「追加した漫画を整理」から進みます。
初期設定と制約は [ノーマライズガイド](docs/NORMALIZATION.md) を参照してください。

**現在: 0.1.0rc1（初回正式版のリリース候補）。** 必須機能と公開準備の検証を終え、正式公開を残しています。
デモは外部サービスやAPIキーなしで起動します。漫画のダウンロード機能は含みません。

[English README](README.en.md) · [ロードマップ](docs/ROADMAP.md) · [導入・更新・復旧](docs/OPERATIONS.md) · [対応環境](docs/SUPPORT.md) · [リリースノート](docs/RELEASE_NOTES.md)

## 0.1.0の完成範囲

初回正式版は、蔵書の読み取り・整理・メタデータ補完と、確認付きCLIによるKomga反映・復元を対象にします。
巻単位の著者・役割の反映まで実装済みです。新規導入・dev8からの更新・バックアップ復元と配布物を検証し、rc1を用意しました。
未リリースの変更として、追加・変更分の確認と、明示的に有効化したWebからのノーマライズ・復元を追加しました。
既存Toolsの機能を先に揃える方針で、ポータルの情報連携・新刊・AI補助・ページ修正・自動更新も追加しました。機能と検証範囲は [機能対応表](docs/FEATURE_PARITY.md) と [操作ガイド](docs/TOOLS.md) を参照してください。
対応先と完了条件は [ロードマップ](docs/ROADMAP.md) にまとめています。未実装の項目は、以下の利用可能な機能とは分けて記載します。

## いま使える機能

上部の共通ヘッダーから「ポータル」「蔵書」「発見」「新刊」「今期アニメ」「ランキング」「完結移動」「表紙修正」「管理」を開けます。蔵書検索もヘッダーにまとめています。
「管理」は「メンテナンス」「環境設定」「自動更新」「処理履歴」のタブに分けています。ノーマライズ・補完・手動修正などの実行はメンテナンス、接続・情報源・整理ルールは環境設定にあります。
ヘッダーとポータルの機能一覧は、環境設定の「表示するメニュー」で項目と順番を変えられます。
ポータルからは蔵書検索・完結作品の一覧と、漫画を追加したときのノーマライズへ進めます。

- ポータル・蔵書一覧・作品名/著者/読みの検索・五十音フィルター
- ローカル書庫の再帰スキャン（CBZ / ZIP / CBR / RAR / 7z / EPUBの所在・巻数）
- ZIP/CBZ・RAR/CBR・7zのComicInfo.xml、EPUBの巻数、作品フォルダのseries.jsonの読み取り
- 半角/全角カナの正規化と、読みからの五十音分類
- 状態フォルダ名、作品フォルダ名、巻名、状態別階層の設定
- 整理プレビュー、巻数不明・整理先重複・既存ファイルの衝突検出
- 要確認を優先する20件ずつの配置確認、全件検索・状態別表示、前回結果の再利用
- CLIで確認した計画の命名・移動、全対象のバックアップ、上書きしない復元（明示的な有効化が必要）
- バックアップ・移動先の保存確定、直前の操作記録の保持、強制終了後の復元・生成取り消し
- 完結フォルダへの配置と、検証済みKomga 1.29.0 / 1.29.1の既読位置・ロックした情報の引き継ぎ
- ZIP/CBZ・RAR/CBR・7zから別の保存先へCBZ生成、ページ名と格納順の正規化、ComicInfo.xmlの保持
- スキャン・整理プレビューの処理ジョブ、冊数の進捗、結果・操作履歴の保存
- 未変更の書庫メタデータの永続キャッシュ、60作品ずつの一覧表示、再スキャン中の閲覧
- 作品名・著者・読み・状態・あらすじ・出版社の手動編集、差分確認、編集の解除
- 楽天Booksの書誌検索・代表巻の対応付け、項目を選んだ補完・差分レビュー、情報源と取得日時の保存
- 選んだ最大60作品の補完候補をバックグラウンドで取得し、確認後に不足項目を一括保存（曖昧な作品は個別確認）
- 補完候補の作品名・確認済み別名・著者・出版社・版違いの照合理由、レーベルの明示表記からの読者層候補
- AniListの漫画検索とタグ・ジャンル補完、複数情報源の対応付け、分類の手動編集・蔵書検索
- CLIで選んだ作品情報をKomga 1.29.0 / 1.29.1へ反映、値とロックの差分確認・バックアップ・衝突を検出する復元
- 手動で完結にした作品のKomgaタグ・連載状態をCLIで反映・復元（他の分類、休載・打ち切り状態を保持）
- 巻ごとの著者・原作/作画などの役割をCLIで反映・復元（既存一覧を保持、対象巻を選択可能）
- 作品情報・完結情報・巻の著者の一括反映と逆順の復元（既定はKomgaの空欄だけ、書庫は属性で照合）
- Komgaの並び順からの読みの一括取り込みと一括取り消し（設定済みの読みは上書きしない）
- Komga APIキー接続の確認・ライブラリ一覧・作品一覧API
- Komgaとローカルのパス対応付け、対応する作品の確認
- ライト/ダーク表示、レスポンシブUI、架空サンプルCBZの生成
- Docker Compose、CLI、基本テスト、機能/メタデータ取得元の拡張口

既定の設定は**プレビュー専用**です。CLIの実行は `execution.enabled: true`、Webのノーマライズ実行は追加で `execution.web_enabled: true` にして有効にします。
実行はPOSIX・同一ファイルシステム内・作品単位に限り、全対象をバックアップしてから移動します。
電源断対策の保存順序・停止後の復元と検証範囲は [保存と中断からの復元](docs/DURABILITY.md) を参照してください。
移動のKomga連携時は明示的にスキャンを要求します。CBZ生成では元書庫を保持し、別の保存先へ出力します。
Komgaへ未登録の取り込み用ライブラリでは、Webからバックアップ付きで元書庫をCBZに置き換えられます。
Komga 1.29.1では、内容を保つ移動後に同じパスで再生成し、書籍ID・既読情報・読了日を照合します。別ライブラリへの任意の既読移行、ページ数が変わる削除は対応範囲外です。
Komgaへの作品情報は、CLIまたは管理の「Komgaへの反映」で対象と項目を確認して実行します。[Komga反映・復元ガイド](docs/KOMGA_METADATA.md) を参照してください。
完結タグ・連載状態の同期は [完結情報ガイド](docs/COMPLETION.md) を参照してください。移動も行う場合は移動後に同期を計画します。
巻単位の著者は [著者・役割ガイド](docs/AUTHORS.md) を参照してください。既定は追加方式、1計画50巻までです。
手動編集はこのツールの実行データへ保存し、蔵書検索・五十音分類・整理プレビューに反映します。
楽天Booksの補完は、このツールの作品情報へ保存します。デモでは架空の書誌データを使います。
実際の検索には利用者のアプリID・アクセスキーが必要です。[書誌検索・補完ガイド](docs/METADATA_SOURCES.md) を参照してください。
楽天APIクライアントは模擬応答で検証しており、実APIへの認証接続は未検証です。
AniListの補完は `anilist.enabled: true` で有効にします。APIキーは不要で、作品名で検索します。
楽天Booksと一緒に対応付けを保持でき、ジャンル・タグを確認・修正して保存できます。デモでは架空データを使います。
公開APIへ架空の検索語で接続し、0件の正常応答を確認しました。実在作品の照合は未検証です。
ポータル内のアニメ・新刊・発見・ランキング・読書状況、管理内のAI・表紙/ページ・完結移動・スマート本棚・自動更新を追加しました。ポータルと作品詳細で書庫の表紙を利用できます。
楽天・AIの実認証、実作品の照合、Windows実機などの未検証範囲は [機能対応表](docs/FEATURE_PARITY.md) に記録しています。

## 起動

Linux、Python 3.11以上（3.11 / 3.13で検証）、システムのlibarchiveが必要です。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/manga-kit serve
```

<http://127.0.0.1:5080> を開いてください。起動時の既定はサンプル蔵書です。
`serve` は開発用サーバーです。コンテナではGunicornを使います。
開発時は最後のインストールに `-e` を追加できます。詳細な [導入・更新・復旧手順](docs/OPERATIONS.md) と [検証範囲・制約](docs/SUPPORT.md) を用意しています。

依存バージョンはLinux向けの `requirements.lock` に固定し、[第三者ライセンス](THIRD_PARTY_NOTICES.md) と全文を同梱しています。
ソース配布物とwheel、チェックサムの生成手順は [配布物の生成](docs/OPERATIONS.md#配布物を検証生成する) を参照してください。

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

普段は「自動補完」で対象を選び、候補取得 → 内容確認 → 不足情報の一括保存と進みます。
取得できない項目や誤った情報は「管理」→「作品情報の手動修正」で作品を選び、編集 →「変更内容を確認」→「この内容で保存」と進みます。
読みは半角・全角カナをひらがなに揃えます。「読み取った情報に戻す」で編集を解除できます。
別の操作や書庫側で情報が変わった場合は、差分を再確認してから保存します。

蔵書の再スキャンは「管理 → メンテナンス」、整理プレビューは整理ルールの画面から実行します。処理履歴には結果を記録します。
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

### Komgaへ作品情報を反映する

[Komga反映・復元ガイド](docs/KOMGA_METADATA.md) の設定と条件を確認します。
ローカルの作品IDを指定し、項目ごとの値とロックを確認してから実行します。Komgaの作品はパス対応から特定します。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-metadata \
  --series LOCAL_SERIES_ID --field reading --field publisher --field genres --field tags
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

タグ・ジャンルは標準で追加方式です。置き換えやロック済みの値の変更は計画時の追加指定が必要です。
書庫・書籍ID・既読位置を変更せず、後から編集された対象項目は復元時に上書きしません。

全作品をまとめる場合は `prepare-komga-batch --mode metadata|completion|authors --all` を使います。
既定ではKomgaの空欄だけを埋め、書庫は読まずにサイズ・日時とKomgaのパス・ハッシュで照合します。
Komgaの並び順に読みがある場合は `prepare-reading-import --all` で未設定の読みを取り込めます。
詳細は [Komga反映・復元ガイド](docs/KOMGA_METADATA.md) を参照してください。

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
隔離したARM64環境の **Komga 1.29.0 / 1.29.1** で、接続・作品照合・命名・完結フォルダへの移動・復元を検証しました。
1.29.1は1.29.0からWeb画面の修正だけを含む版で、同じ実機テスト一式が両方で成功しています。
複数ユーザーの未読・途中・読了、最新の既読位置、ロックした作品情報を確認しています。
移動時にはKomga側の書籍/作品IDや一部の管理日時が変わります。ID参照の外部連携は未検証です。

移動を連携して実行する場合は1.29.0 / 1.29.1に限定し、ハッシュ計算済み・自動スキャン無効・ゴミ箱自動削除無効・自動変換無効を要求します。
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
