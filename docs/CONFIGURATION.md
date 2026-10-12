# 設定

指定順: CLIの `--config` → `MANGA_KIT_CONFIG` → 作業ディレクトリの `config.yaml`。
既定ファイルがない場合はデモ設定で起動する。明示的に指定したファイルがない場合はエラーにする。
YAMLは安全なローダーで読み、未知の設定項目・変数・階層の逸脱を検証する。

| 項目 | 用途 |
| --- | --- |
| `app.name` | 画面の名称 |
| `app.demo` | 架空データ表示。falseでローカルファイルをスキャン |
| `library.root` | 漫画の保存先。相対パスは設定ファイル基準 |
| `library.max_files` | スキャン上限。既定20000、最大100000 |
| `data.root` | 編集・処理履歴の保存先。既定 `./var/state`、設定ファイル基準 |
| `layout.status_folders` | 連載中・完結・未分類のフォルダ名 |
| `layout.series` | 作品フォルダ名 |
| `layout.book` | 巻ファイル名。拡張子の変数は必須 |
| `layout.ongoing/completed/unknown` | 状態ごとの階層 |
| `komga.base_url` | APIを提供するKomgaのURL。サブパス込みで指定可能 |
| `komga.api_key_env` | APIキーを読む環境変数名 |
| `komga.timeout_seconds` | 接続タイムアウト。既定10秒 |
| `komga.path_mappings` | Komgaの絶対パスと、ローカルのライブラリ内パスの対応 |

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
  "publisher": "サンプル出版"
}
```

必要な項目だけでよい。`status` は `ongoing` / `completed` / `unknown`。
読みがない場合は、作品名がカナで始まればその行、漢字や英字で始まれば「その他」になる。
CBZ/ZIP内のComicInfo.xmlからはSeries、Writer、Summary、Publisher、Numberを読む。
数値巻と小数巻に対応し、全角数字は正規化する。
メタデータサイズは256KiBまで。シンボリックリンクはスキャンしない。

UIのルール変更は一時プレビュー。適用する設定はYAMLへ保存して再起動する。

## 手動編集と実行データ

メタデータ画面で作品名を選ぶと、作品名・著者・読み・状態・あらすじ・出版社を編集できる。
差分を確認して保存すると、書庫からの読み取り結果より優先して、このツールの表示・検索・整理計画に使う。
読みの半角・全角カナはひらがなへ揃える。書庫のComicInfo.xml、series.json、Komgaは書き換えない。
「読み取った情報に戻す」で編集を解除できる。操作履歴は残る。
保存時には編集の版と書庫情報の指紋を再確認し、変更があれば409応答で再レビューを求める。

`data.root` に `state.sqlite3` とSQLiteの補助ファイルを保存する。
デモと実ライブラリ、異なるライブラリの履歴・編集は分けて管理する。同じ保存先で再起動すれば保持される。
ライブラリのルートを変更すると別ライブラリとして扱う。保存先の移設時はアプリを停止し、実行データのフォルダ全体をコピーする。
設定ファイル、実行データ、漫画ライブラリはいずれも公開用リポジトリへ追加しない。

Composeでは `MANGA_KIT_DATA_ROOT=/state` を設定し、名前付きボリュームを使う。
読み取り専用コンテナでも `/state` はアプリの非rootユーザーが書き込める。
ボリュームを削除すると編集と履歴も消えるため、バックアップはコンテナ停止後に行う。

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
