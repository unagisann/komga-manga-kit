# 構成と拡張

```text
src/manga_kit/
  app.py          アプリ生成、認証、APIエラー、安全なレスポンス
  config.py       設定の読み込み・検証。漫画の位置はソースから独立
  features.py     機能登録とナビゲーション
  web.py          各画面のBlueprintとAPI
  komga.py        読み取り専用のKomgaクライアント
  models.py       蔵書・巻・スキャン結果の共通モデル
  library.py      ローカルスキャン、短期メモリキャッシュ
  japanese.py     カナ正規化・五十音・検索キー
  organizer.py    ファイル変更を伴わない配置計画
  providers.py    メタデータ取得元のインターフェースと読み取り
  sample.py       架空のデータ・検証用CBZの生成
  cli.py          UI・スキャン・計画・サンプル生成の入口
  templates/     共通シェル、部品、各画面
  static/        共通デザイントークン・CSS・JS
tests/           独立した一時フォルダと模擬APIによるテスト
docs/            設定・構成・ロードマップ
config.yaml      利用者の設定（Git対象外）
var/             サンプルなどの実行データ（Git対象外）
```

## 機能を追加する

新機能のBlueprintを作り、`Feature` を `built_in_features()` へ追加する。
機能ID、ラベル、画面のエンドポイント、アイコン、Blueprintの5点で登録する。
`base.html` が登録内容からナビゲーションを組み立てる。
テンプレートは `base.html` を継承し、共通部品は `components.html` を使う。
CSSは `tokens.css` の色・余白・書体・角丸を参照する。

```python
from flask import Blueprint, render_template
from manga_kit.features import Feature

discovery = Blueprint("discovery", __name__)

@discovery.get("/discovery", endpoint="index")
def index():
    return render_template("discovery.html")

feature = Feature("discovery", "発見", "discovery.index", "search", discovery)
```

v0.2で機能が増えたら、`web.py` の各Blueprintを `features/` 配下へ分割できる。
登録側と共通UIへの変更は最小限にする。

## メタデータ取得元を追加する

`MetadataProvider` の `id`・`label`・`inspect(path)` を実装する。
戻り値は `MetadataProposal(source, fields)`。取得元はファイルやKomgaへ書き込まない。
`create_app()` の取得元一覧へ登録すると、スキャン時に呼ばれる。
外部API向けの検索/作品ID対応付けは別インターフェースとしてv0.1で追加する。
スキャン中に蔵書全件について外部APIを呼ぶ設計にはしない。

優先順位: 作品フォルダの `series.json` → 書庫のComicInfo.xml → フォルダ/ファイル名。
複数巻で作品メタデータが異なる場合、作品名は最後に読み取った巻、それ以外は最初の非空値を使う。
巻数は各書庫の `Number` を優先する。後続の補完UIでは巻ごとの違いもレビューできるようにする。

## 整理・実行の分離

`plan_organization()` は共通モデルと設定から整理計画を返す純粋な処理。
実フォルダを指定した場合のみ、既存ファイルとシンボリックリンクによる整理先の逸脱を確認する。
大文字小文字・互換文字が異なる同一パスも衝突扱いにする。
読みが分からない作品は「その他」、巻数が分からない作品は「要確認」にする。

書庫変換・移動・Komga更新の実行は別のジョブサービスとして追加する。
実行時には計画後のファイル変化を再確認し、変更前情報と復元手段を保持する。
既読引き継ぎはKomga APIで扱う方針。DBの直接参照を必須にしない。

## API（現段階）

| API | 用途 |
| --- | --- |
| `GET /healthz` | 稼働確認 |
| `GET /api/library` | ローカル/デモの蔵書と集計 |
| `POST /api/library/refresh` | メモリキャッシュを再読み取り |
| `POST /api/organize/preview` | 設定差分による整理計画 |
| `GET /api/settings` | キーを含まない設定概要 |
| `POST /api/komga/test` | 明示的な接続確認 |
| `GET /api/komga/series?page=0&size=50` | Komgaの作品概要（最大100件/頁） |

整理プレビュー例:

```json
{"layout": {"completed": "{author}/{series}", "series": "{title}"}}
```

現段階は短期メモリキャッシュのみ。永続ジョブ・DB・定期実行は未導入。
