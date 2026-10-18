# 既存Tools相当の操作と設定

現在の機能対応は [機能対応表](FEATURE_PARITY.md) を参照。
ポータルを残し、発見・新刊・今期アニメ・ランキング・完結移動・表紙修正と管理への入口、蔵書検索を上部の共通ヘッダーにまとめる。本文上の2段目のメニューは置かない。読書状況はヘッダーに表示しない。
自動補完を基本にし、手動修正は管理に置く。機能を揃えた後で、必要性と配置を見直す。

## 読む・見つける

- ポータル: 最近追加した作品ごとの最新巻、完結作品の抽選、所持作品のアニメ・新刊・紹介記事。
- 蔵書 `/library`: 既存Toolsのライブラリ探索と同じ。読者層・状態・年代のチップ（件数は蔵書全体）、検索語、作者（複数作品のある人）・出版社（上位24社）、並び順で絞り込み、60件ずつ「もっと見る」で追加する。条件はURLに残る。
- 今期アニメ `/anime`: 当季と前季から継続中の作品。原作・漫画版の別名でも蔵書と照合する。
- 新刊 `/new_volumes`: 連載中の作品について紙・電子の最新巻と予約を確認。関連本・分冊版・不確かな発売日は確認用に残す。
- 発見 `/discovery`: ぶくまるの紹介作品を表紙で探す。今日のおすすめは蔵書3冊・未所持2冊を基本に、同じ作品の重複を避けて日本時間の日付で選ぶ。候補が少ない場合はある作品だけを表示する。テーマ／最近完結の特集を選び、所持状況と作品名・著者で絞り込める。所持判定は個別に修正でき、Komga接続時は「Komgaで読む」、未接続時は作品詳細へ進む。「記事と蔵書を更新」は蔵書を読み直してから紹介記事を更新する。既存キャッシュに特集名がない場合は「紹介された作品」にまとめ、更新後にテーマと完結特集へ分かれる。楽天Kobo・マンバ・ナタリーは既存の `?source=rakuten` / `manba` / `natalie` で利用できる。
- マンガ大賞 `/tsugimanga`: 年・部門別。所持判定が誤っている作品は蔵書を検索して対応を保存する。
- 読書状況 `/reading`: 各アカウントの途中・読了・60日以上の中断、巻の進捗・しおり。

情報源の更新と蔵書情報の更新はボタンから行う。通常のページ表示で書誌検索やフィード更新は始めない。
紹介画像は各情報源の画像サーバーからブラウザーで読み込む。
Komga未接続の「最近追加」は、蔵書情報を更新して初めて確認した日時であり、過去の登録日時は推定しない。

### 発見ページを実際の記事・蔵書で使う

サンプルモード（`app.demo: true`）は、更新ボタンも架空の記事・蔵書を使う。
実際のぶくまるの記事を取得するには、設定ファイルでサンプルモードを解除し、蔵書・実行データを別の保存先にして再起動する。

```yaml
app:
  demo: false
library:
  root: /path/to/manga
data:
  root: /path/to/manga-kit-state
tools:
  discovery_enabled: true
```

`/discovery` の「記事と蔵書を更新」で、公開RSSの取得、作品名・著者による所持照合、特集・表紙の表示まで実行する。
ぶくまるの取得にAPIキーは不要。取得結果と手動で修正した所持判定は実行データへ保存する。
蔵書ファイルの変更や `execution.enabled` の有効化は必要ない。
Komga未接続でも記事・照合・ローカル表紙は使え、所持作品から作品詳細を開ける。
「Komgaで読む」を使う場合は「接続・情報源」でKomga URLとAPIキーを設定して、もう一度「記事と蔵書を更新」を押す。

## 接続・情報源・認証情報

「管理 → 設定 → 接続・情報源・AI・認証情報を設定」で有効化する。既定はすべて無効。
初期値は設定ファイル、Webで変更した非秘密の設定はライブラリごとの実行データに保存する。
漫画・実行データ・設定の保存先は分ける。秘密情報をリポジトリへ保存しない。

```yaml
rakuten:
  enabled: false
  application_id_env: RAKUTEN_APPLICATION_ID
  access_key_env: RAKUTEN_ACCESS_KEY
  origin: ""
tools:
  kobo_enabled: false
  anime_enabled: false
  discovery_enabled: false
  rankings_enabled: false
  mangaupdates_enabled: false
  ai_enabled: false
  notify_enabled: false
  ai_provider: chat
  ai_url: ""
  ai_model: ""
  ai_key_env: MANGA_KIT_AI_KEY
  notify_url_env: MANGA_KIT_NOTIFY_URL
  public_komga_url: ""
  credentials_file: "" # Webで登録するときだけ、設定専用フォルダの絶対パスを指定
  reading_accounts: []
```

環境変数を優先する。Webで登録した値は `credentials_file` の0600ファイルへ保存し、内容を応答・履歴・DBへ戻さない。
保存先は漫画・実行データの外にする。未入力の項目は保持する。環境変数の設定はWebから変更しない。
楽天BooksとKoboは同じアプリID・アクセスキーを使うが、対応付けは別々に保持する。KoboのIDはISBNとして扱わない。
MangaUpdatesは書誌エンドポイントのみを使い、配信・取得元の検索は行わない。

読書状況は、アカウント本人が発行したAPIキーを個別に登録する。
管理者のキーを使って他人の進捗が取得できるとは想定しない。

```yaml
tools:
  reading_accounts:
    - name: 自分
      api_key_env: KOMGA_READER_A
    - name: 家族
      api_key_env: KOMGA_READER_B
```

## 管理・補完・反映

- 補完 `/metadata`: 不足項目の候補、未登録の対応付け構築、全作品の対応付け再確認。最大60作品ずつ。
- 完結移動 `/completion`: 完結にした作品の移動先と衝突を確認して実行・復元。
- Komga反映 `/komga-sync`: 作品情報・読み・タグ、巻の著者、完結タグ・連載状態。既定は空欄の補完と一覧への追加。
- スマート本棚 `/smart-shelves`: 変更を確認して同期。Manga Kitが作成したIDだけを更新し、個人の同名本棚があれば停止する。取得元が未更新なら保持。
- AI補助 `/ai`: 設定したモデルで提案を取得し、根拠・差分を確認して保存。項目を編集・選択でき、保存前へ戻せる。別の作品と同じあらすじ、日本語以外、HTML、後の巻の記述も点検する。

AIには作品名・著者・出版社・現在のあらすじ・対応付けの出典を送り、漫画本文やローカルパスは送らない。
Chat Completions互換は対応するAPIのベースURL、Geminiは `https://generativelanguage.googleapis.com/v1beta` を指定する。
モデルは保存した接続先から一覧を取得できる。GeminiのWeb検索は提案取得時に個別に選ぶ。
根拠がない・著者や作品が異なる提案を自動で確定しない。ローカルで保存した後のKomga反映は別の確認操作。

書き込みは `execution.enabled: true` と `execution.web_enabled: true` が必要。
ローカル接続、または `MANGA_KIT_ADMIN_TOKEN` の管理者認証を使い、表示した差分を確認して実行する。
既定のプレビュー、ロック済み項目の保護、衝突時の停止、履歴からの復元を維持する。

## 表紙とページ

`/pages` で巻を選び、残すページ・順序・先頭の表紙を指定して確認する。
削除・並べ替え時はComicInfo.xmlのページ数・ページ参照・表紙参照を更新する。画像のバイト列は再圧縮しない。
元書庫はバックアップし、`/pages/trash` から操作を確認・復元できる。
バックアップの完全削除は別の操作で、現在の書庫は消さない。完全削除後はその操作を復元できない。

Komga 1.29.1では同じページ数の表紙・順序変更まで対応する。
ページ数が変わる削除は、全ユーザーの読了日を保持しながら位置を再対応させるAPIがないため停止する。
Komgaで管理している漫画の接続設定を外して制約を回避しない。

## 自動更新と取り込み後処理

`/automation` で時刻（日本時間）とステップを保存する。アプリ起動中の日次実行、今すぐ実行、ノーマライズ後の実行を選べる。
ホストの既存systemdタイマーや既存Toolsは操作しない。
既定は候補と計画の作成まで。自動反映には実行設定と「自動反映する」の両方を有効にする。
失敗したステップは結果を残し、完了したステップを繰り返さず再開する。
途中でKomga反映が止まった場合は、記録した操作リンクから確認・復元してから再開する。

通知は `notify_enabled` を明示的に有効にし、環境変数のHTTPS Webhookへ `{content: 新刊案内}` をPOSTする。
同じ作品・最新巻の送信成功を記録し、再通知を抑える。送信失敗は新刊の取得結果と分けて表示する。

## 1冊だけCBZにする

Windowsを含む単体変換用の経路。書き込みを有効にしていなくても最初の確認はできる。
ZIP/CBZは標準ライブラリ、RAR/7zは別途libarchiveが必要。Windows実機での検証は未実施。

```console
manga-kit --config /path/to/settings.yaml convert-one source.zip --output converted.cbz
manga-kit --config /path/to/settings.yaml convert-one source.zip --output converted.cbz --confirm 表示したdigest
```

2回目の生成には設定ファイルで `execution.enabled: true` が必要。
元書庫を保持し、存在しない別名のCBZへ出力する。出力先のフォルダを事前に作る。
出力を途中の状態で公開しないため、同じ保存先内のハードリンクに対応したファイルシステムを使う。

## APIの参照仕様

- [Komga 1.29.1 OpenAPI](https://github.com/gotson/komga/blob/1.29.1/komga/docs/openapi.json)
- [楽天Kobo検索](https://webservice.rakuten.co.jp/documentation/kobo-ebook-search)
- [MangaUpdates API](https://api.mangaupdates.com/)
- [Gemini生成API](https://ai.google.dev/api/generate-content) / [モデル一覧](https://ai.google.dev/api/models)
