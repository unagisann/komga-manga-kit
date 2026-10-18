# 設定

指定順: CLIの `--config` → `MANGA_KIT_CONFIG` → 作業ディレクトリの `config.yaml`。
既定ファイルがない場合はデモ設定で起動する。明示的に指定したファイルがない場合はエラーにする。
YAMLは安全なローダーで読み、未知の設定項目・変数・階層の逸脱を検証する。

| 項目 | 用途 |
| --- | --- |
| `app.name` | 画面の名称 |
| `app.demo` | 架空データ表示。falseでローカルファイルをスキャン |
| `library.root` | 漫画の保存先。相対パスは設定ファイル基準 |
| `library.max_files` | スキャン上限。既定50000、最大100000。冊数が上限の90%を超えると警告する |
| `data.root` | 編集・処理履歴の保存先。既定 `./var/state`、設定ファイル基準 |
| `execution.enabled` | 整理実行・復元・CBZ生成/取り消し・Komga情報の反映・復元を有効化。既定false |
| `execution.web_enabled` | Webの確認付きノーマライズ実行・復元。既定false。enabledもtrueが必要。リモート実行は管理者認証が必要 |
| `layout.status_folders` | 連載中・完結・未分類のフォルダ名 |
| `layout.series` | 作品フォルダ名 |
| `layout.book` | 巻ファイル名。拡張子の変数は必須 |
| `layout.ongoing/completed/unknown` | 状態ごとの階層 |
| `komga.base_url` | APIを提供するKomgaのURL。サブパス込みで指定可能 |
| `komga.api_key_env` | APIキーを読む環境変数名 |
| `komga.timeout_seconds` | 接続タイムアウト。既定10秒 |
| `komga.path_mappings` | Komgaの絶対パスと、ローカルのライブラリ内パスの対応 |
| `rakuten.enabled` | 楽天Booksの書誌検索を有効化。既定false、デモでは架空データを使用 |
| `rakuten.application_id_env` | アプリIDを読む環境変数名。既定 `RAKUTEN_APPLICATION_ID` |
| `rakuten.access_key_env` | アクセスキーを読む環境変数名。既定 `RAKUTEN_ACCESS_KEY` |
| `rakuten.origin` | 任意。楽天に登録した自分のサイトのオリジンをOrigin/Refererへ設定 |
| `rakuten.timeout_seconds` | 楽天Books接続のタイムアウト。既定10秒、1〜30秒 |
| `anilist.enabled` | AniListの漫画検索とタグ・ジャンル補完を有効化。既定false、デモは架空データ |
| `anilist.timeout_seconds` | AniList接続のタイムアウト。既定10秒、1〜30秒 |

環境変数 `MANGA_KIT_LIBRARY_ROOT`、`MANGA_KIT_DEMO`、`KOMGA_URL` は対応するYAML値より優先する。
`MANGA_KIT_DATA_ROOT` は `data.root` より優先する。実行データとライブラリの保存先は重複できない。
APIキーと任意の認証用 `MANGA_KIT_ADMIN_TOKEN` は環境変数からのみ取得する。
ローカル起動は `.env` を自動ロードしない。シェル側で環境変数を設定する。
Composeでは利用者が作成した `.env` をコンテナへ渡す。

## フォルダ例

状態と五十音:

```yaml
layout:
  ongoing: "{status}/{series}"
  completed: "{status}/{kana_row}/{series}"
  unknown: "{status}/{series}"
```

作者ごと:

```yaml
layout:
  series: "{title}"
  ongoing: "{author}/{series}"
  completed: "{author}/{series}"
  unknown: "{author}/{series}"
```

フラット:

```yaml
layout:
  series: "{title}"
  ongoing: "{series}"
  completed: "{series}"
  unknown: "{series}"
```

状態の読み取りは、現在のパスの先頭要素が設定済み状態フォルダと一致するか、`series.json` の `status` で判断する。
作者ごと/フラットな既存階層では `series.json` で状態を補う。
各フォルダに直接置かれた漫画ファイルを一作品として扱い、入れ子のフォルダは別作品として検出する。

## series.json

作品の漫画ファイルと同じフォルダに置く。

```json
{
  "title": "雨あがりの喫茶室",
  "author": "森野しおり",
  "reading": "アメアガリノキッサシツ",
  "status": "ongoing",
  "summary": "雨の日だけ開く喫茶室の物語。",
  "publisher": "サンプル出版",
  "genres": ["日常"],
  "tags": ["喫茶店", "仲間"]
}
```

必要な項目だけでよい。`status` は `ongoing` / `completed` / `unknown`。
読みがない場合は、作品名がカナで始まればその行、漢字や英字で始まれば「その他」になる。
五十音の行を使う整理先へ新たに移す場合、読みのない「その他」は要確認として整理計画を止める。

Komgaが読むMylar形式（`{"version": ..., "metadata": {...}}`）の `series.json` も読み取る。
`metadata.name` を作品名、`publisher` を出版社、`description_formatted`（なければ `description_text`）をあらすじにする。
`volume` が1以外の場合はKomgaと同じく「作品名 (year)」とする。`status`（Ended/Continuing）は整理先を変えるため取り込まない。
上記の独自形式はこのツール専用の形式で、Mylar形式ではない。KomgaのMylar取り込みを有効にしたライブラリでは、
Komgaが読み取りエラーを記録してその作品の `series.json` を使わない。Komgaにも読ませる場合はMylar形式を使う。
`genres`・`tags` はそれぞれ100個以内、各1〜100文字の文字列の配列。空配列で分類を解除できる。
ZIP/CBZ・RAR/CBR・7z内のComicInfo.xmlからはSeries、Writer、Summary、Publisher、Number、Genre、Tagsを読む。
書庫内のサブフォルダとXML名前空間にも対応。RAR/7zの読み取りにはlibarchiveが必要。
Genre・Tagsはカンマで区切り、前後の空白と大文字小文字だけが異なる重複を取り除く。
数値巻と小数巻に対応し、全角数字は正規化する。巻数は0〜99999、入力64文字・小数32桁まで。
EPUBではcontainer.xmlからOPFを特定し、`group-position` を巻数として読む。内部は変更しない。
書庫内の巻数がなければ、ファイル名の `第01巻`・`v02`・末尾の `（3）` を優先して巻数を読む。
その表記もなければ、末尾の `_01`・`_120` のようなアンダースコア付き2〜3桁の整数を使う。
作品名の裏付けがなく、アンダースコアの直前が数字の場合は、年月日や番号列との誤判定を避けて未判定にする。
フォルダの作品名がファイル名の先頭と一致するときは、作品名を除いた末尾から `作品1`、`作品_1.5`、
`作品（ 02 ） - 著者` も読む。作品名が `作品86` なら `作品86_01.zip` は1巻、`作品86.zip` 自体は巻数不明になる。
既知の作品名の裏付けがない `作品_2024_01.zip`、`作品86_01.zip`、`作品_1.zip`、単なる `作品 01.zip` からは推定しない。
巻数の推定結果は整理プレビューで確認する。ComicInfo.xmlのNumber・EPUBの巻数はファイル名より優先し、食い違いは警告する。
複数のComicInfo.xmlや異なるEPUB巻数を検出した場合は警告し、ファイル名で判定できればその値を使う。
隠しファイル、`__MACOSX`、リンク、通常ファイル以外はスキャンしない。
既存KomgaToolとの対策の対応と残る差分は [互換性の比較](COMPATIBILITY.md) を参照する。
メタデータの読み取りは1回あたり合計256KiBまで。

UIのルール変更は一時プレビュー。適用する設定はYAMLへ保存して再起動する。
ノーマライズの実行は起動時のルールを使う。[ノーマライズガイド](NORMALIZATION.md) を参照。

## 手動編集と実行データ

メタデータ画面で作品名を選ぶと、作品名・著者・読み・状態・あらすじ・出版社・ジャンル・タグを編集できる。
画面のジャンル・タグは1行に1項目で入力する。検索にも利用できる。
差分を確認して保存すると、書庫からの読み取り結果より優先して、このツールの表示・検索・整理計画に使う。
読みの半角・全角カナはひらがなへ揃える。書庫のComicInfo.xml、series.json、Komgaは書き換えない。
「読み取った情報に戻す」で編集を解除できる。操作履歴は残る。
保存時には編集の版と書庫情報の指紋を再確認し、変更があれば409応答で再レビューを求める。

`data.root` に `state.sqlite3` とSQLiteの補助ファイルを保存する。
デモと実ライブラリ、異なるライブラリの履歴・編集は分けて管理する。同じ保存先で再起動すれば保持される。
ライブラリのルートを変更すると別ライブラリとして扱う。保存先の移設時はアプリを停止し、実行データのフォルダ全体をコピーする。
設定ファイル、実行データ、漫画ライブラリはいずれも公開用リポジトリへ追加しない。
CLIの実行計画・途中状態・元ファイルのバックアップは `data.root/operations` に保存する。
旧形式の状態DBは起動時にスキーマ3へ更新し、既存の編集・履歴・整理記録を保持する。
スキーマ3では書誌検索キャッシュと補完元の対応付けを追加する。更新前のバックアップはアプリ停止後に取得する。
整理実行・復元の設定と制約は [整理実行ガイド](ORGANIZATION.md) を参照する。
CBZ生成も同じ実行データを使う。`prepare-conversion --output` で元ライブラリと実行データに重ならない出力先を指定する。
RAR/7zにはlibarchiveが必要。手順は [CBZ生成ガイド](CONVERSION.md) を参照する。

Composeでは `MANGA_KIT_DATA_ROOT=/state` を設定し、名前付きボリュームを使う。
読み取り専用コンテナでも `/state` はアプリの非rootユーザーが書き込める。
ボリュームを削除すると編集と履歴も消えるため、バックアップはコンテナ停止後に行う。

## 楽天Booksの書誌検索

```yaml
rakuten:
  enabled: true
  application_id_env: RAKUTEN_APPLICATION_ID
  access_key_env: RAKUTEN_ACCESS_KEY
  origin: "" # 必要な場合は、楽天に登録した自分のサイトのオリジン
  timeout_seconds: 10
```

アプリID・アクセスキーは環境変数から読む。値そのものをYAMLへ指定する項目はない。
`app.demo: true` では、この設定にかかわらず架空データを使い、楽天へ通信しない。
実ライブラリではメタデータの作品詳細から検索を明示的に実行する。起動やスキャンでは検索しない。
登録・補完・誤照合の修正手順と検索結果の保存期間は [書誌検索・補完ガイド](METADATA_SOURCES.md) を参照。

## AniListのタグ・ジャンル補完

```yaml
anilist:
  enabled: true
  timeout_seconds: 10
```

APIキーやアカウントは不要。設定後に再起動し、補完画面の「タグ・ジャンル · AniList」を選ぶ。
検索時だけ作品名とページ条件を送る。起動・スキャン・ページ表示では外部通信しない。
`app.demo: true` では有効化設定にかかわらず架空データを使う。
楽天BooksとAniListの対応付けを同時に保持し、保存・解除は選んだ情報源だけに適用する。
詳しくは [書誌検索・補完ガイド](METADATA_SOURCES.md) を参照。

## Komgaのパス対応

たとえばKomgaが `/data/manga`、このツールが `/library` を参照する場合:

```yaml
library:
  root: /library
komga:
  path_mappings:
    - komga: /data/manga
      local: /library
```

`local` は `library.root` 内のパスに限る。相対パスは設定ファイル基準。
複数の対応が重なる場合は、最も深いKomga側のパスを優先する。
POSIXパス、Windowsのドライブパス、ローカルの `file:///` URLに対応する。
パスの部品ごとに照合し、単なる前方一致、`..`、ライブラリ外へのシンボリックリンクは採用しない。

設定画面から明示的に「対応する作品を確認」を押すと、Komgaの作品URLをローカルの作品フォルダへ照合する。
最初の50作品を表示する。APIの `page` / `size` で後続ページを確認できる。
結果は「対応する蔵書あり」「蔵書に作品がありません」「パス対応を確認してください」。自動照合や書き込みは行わない。

この対応と管理者APIキーを使い、`prepare-metadata --series LOCAL_ID --field reading` から
作品情報の反映を計画できる。`execution.enabled: true`、Komga 1.29.0 / 1.29.1、ハッシュ・スキャン等の条件と確認値が必要。
手動編集の保存だけではKomgaへ書き込まない。管理のKomga反映画面から、別途確認して実行できる。手順と復元の制約は [Komga反映ガイド](KOMGA_METADATA.md) を参照する。

## ポータル・情報源・AI

`tools` セクションとWebの非秘密設定、認証情報の専用ファイル、自動更新は [Toolsガイド](TOOLS.md) を参照。すべて既定無効。実行の有効化と保存先はWebから変更しない。
