# 導入・更新・バックアップと復旧

対象は0.1.0rc1。検証環境と制約は [対応環境](SUPPORT.md)、操作の検証結果は [検証記録](VALIDATION.md) を参照する。
コード、設定、実行データ、ライブラリは別の保存先にする。秘密情報は環境変数で設定する。

## Pythonで新規導入

Linux、Python 3.11または3.13、システムのlibarchiveが必要。Debian系では `libarchive13t64` 等、OSに合うパッケージを導入する。
ソース配布物を空のコード用ディレクトリに展開し、次を実行する。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/python -m pip check
.venv/bin/manga-kit serve
```

既定では架空デモ・整理プレビューだけが動く。`http://127.0.0.1:5080` を開く。
`requirements.lock` は検証したLinux向けのバージョン固定で、ハッシュ固定ではない。
wheelを使う場合は最後のインストール対象 `.` をwheelのパスへ変更する。
wheel自体に設定例・運用手順・バージョン固定ファイルは含まないため、同じ版のソース配布物も用意する。

実ライブラリでは、コード外の設定ファイルへ `app.demo: false`、`library.root`、`data.root` を設定する。
両ルートに絶対パスを使うと更新時の取り違えを防げる。`execution.enabled` は通常 `false` のままにする。
設定の詳細は [設定ガイド](CONFIGURATION.md)、書き込みを行う場合は各操作ガイドの条件と確認値を使う。
追加した漫画をWebで整理（取り込み整理）する場合は、[取り込み整理ガイド](NORMALIZATION.md) に従い `execution.web_enabled` も有効にする。

## Composeで新規導入

Docker Compose 2.24.0以上を使う（[`env_file.required` の対応条件](https://docs.docker.com/reference/compose-file/services/#required)）。ソース配布物のディレクトリで実行する。

```bash
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:5080/healthz
```

公開先はループバック、実行ユーザーはUID 10001、コンテナのルートは読み取り専用。
実行データは名前付きボリュームに保持する。実ライブラリはREADMEの設定例を使い、`/library:ro` へ別途マウントする。
CLIのファイル移動を行う場合だけ、対象ライブラリへの書き込み権限と操作設定を明示的に変更する。
ボリューム削除を伴う `docker compose down -v` は通常の停止・更新に使わない。
外部へ公開する場合は認証トークンとTLS終端を設定する。既定のループバック接続での導入を検証している。

## 蔵書の読み取りと表示速度

最初のスキャンでは書庫の中のComicInfo.xml等を読み取る。冊数・ページ数・保存媒体によって時間がかかる。
読み取りに成功した組み込み取得元の結果は `data.root` のSQLiteへ保存する。以降の通常閲覧では、
変更のない書庫は開き直さず、一覧とファイル属性・sidecarを確認する。キャッシュは再起動後も使える。
Webの定期的なファイル確認はバックグラウンドで行い、完了まで前回の結果を表示する。
蔵書一覧とメタデータ一覧は60作品ずつ表示する。検索・五十音・状態の絞り込みは全作品が対象。

蔵書整理は前回の確認結果を表示し、「配置を確認」で更新する。要確認を優先した20件ずつの表示で、
状態・検索・ページ送りを切り替えても再スキャンや配置計算を行わない。更新時は現在のファイルと属性を確認し、
未変更の書庫メタデータを使って全冊の件数・衝突をバックグラウンドで計算する。
確認時点と現在の状態は異なる場合があるため、蔵書・ルールの変更後は結果を更新する。

「読み直す」や記録付きスキャンは書庫を改めて読み取る明示操作で、冊数の進捗を表示する。
その間も通常の閲覧には前回の結果を使う。初回スキャンが終わっていなければ結果を待つ。
CLIの実行計画・保存前の確認でも書庫を読み直すため、閲覧時と同じ時間とは限らない。
キャッシュは表示の高速化専用で、Komga同期や自動整理を行わない。

## 更新前のバックアップ

1. 実行中のCLI・Webジョブを終え、必要な操作の復元を済ませる。Komga側の同時編集やスキャンも停止する。
2. アプリを停止する。Pythonサーバー/CLIを終了、Composeなら `docker compose stop manga-kit`。
3. 設定ファイル・ローカルの秘密情報・**実行データ全体**・ライブラリ全体を同じ時点で退避する。
4. 現在の版、依存の固定ファイル、コンテナイメージまたはwheelも保持する。

実行データは `state.sqlite3` だけでなく、WAL/SHMがあればそれらと `operations/` 内のバックアップ・計画・進捗を含める。
DBだけや操作フォルダだけをコピーしない。バックアップ先は非公開・アクセス権を制限した場所にする。

Python版は停止後に `cp -a` 等で各ディレクトリを丸ごとコピーする。
Composeの実際のボリューム名はプロジェクト名で変わるので確認する。

```bash
docker compose stop manga-kit
kit_container=$(docker compose ps -aq manga-kit)
kit_volume=$(docker inspect "$kit_container" --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Name}}{{end}}{{end}}')
kit_image=$(docker inspect "$kit_container" --format '{{.Image}}')
kit_backup=/path/to/private/backup-before-upgrade
mkdir -m 700 "$kit_backup"
docker run --rm --network none --user 0 --entrypoint tar \
  -v "$kit_volume:/state:ro" -v "$kit_backup:/backup" \
  "$kit_image" -C /state -cpf /backup/state.tar .
```

同じバックアップに使用中の設定・`.env`（使っていれば）とライブラリを保存する。
バックアップ先はコードの外にする。`state.tar` には私的な編集・情報源・操作記録が含まれる。公開リポジトリへ置かない。
所有者・モードを保持し、UID 10001が実行データを読み書きできる状態を維持する。

## アプリの更新

設定と `data.root`、アプリから見える `library.root` を維持する。
Python版は新しいコード・新しい仮想環境へ同じ版の固定依存とアプリを導入し、同じ設定で起動する。
Composeは新しいコードで、同じComposeプロジェクト名とボリュームを使って `docker compose up --build -d` を実行する。
通常の更新でデータボリュームやライブラリを作り直さない。

`/healthz` の版番号、スキャン冊数、手動編集・情報源・履歴、保存した操作の表示を確認する。
dev8からrc1への更新では状態DB形式は3のまま。旧版で実行済みの整理を新版で復元する経路も検証した。
起動に失敗した場合は更新を止め、版とエラーを確認する。データファイルを手作業で修正しない。

## バックアップから復旧する

アプリを停止し、現在のデータも別途退避する。空の保存先に、同じ時点の設定・実行データ全体・ライブラリを戻す。
Pythonでは元と同じ絶対パス、Composeでは元と同じ `/state` と `/library` にマウントする。
ライブラリのルートパスが変わると別の名前空間になり、元の編集・履歴は表示されない。
自動移行や名前空間の手編集には対応していない。

Composeでは停止状態で空のデータボリュームを準備し、上記で確認したボリューム名とイメージを指定して戻す。
既存データの入ったボリュームに上書き展開しない。

```bash
docker volume create "$kit_volume"
docker run --rm --network none --user 0 --entrypoint tar \
  -v "$kit_volume:/state" -v "$kit_backup:/backup:ro" \
  "$kit_image" -C /state -xpf /backup/state.tar
docker compose up -d
curl -fsS http://127.0.0.1:5080/healthz
```

編集・情報源・履歴、スキャン冊数とファイル内容を確認してから利用を再開する。
版も戻す場合は更新前のアプリと更新前の設定・実行データ・ライブラリをまとめて戻す。
新しいデータを古いアプリに直接読ませる一般的なダウングレードは保証しない。

**物理的に作り直したライブラリでは、復旧前の操作計画を実行・復元できない。**
同じパス・内容でもディレクトリ/ファイルの同一性を検証するためで、履歴としての参照はできる。
復旧後に新しい計画を作る。操作途中に媒体が失われた場合の全自動復旧や、Komga自身のDB復元は対象外。
バックアップを取る前に操作を完了・復元することを基本とする。

## 配布物を検証・生成する

```bash
.venv/bin/python -m pip install 'build>=1.2,<2'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_release.py
cd dist
sha256sum -c SHA256SUMS
```

`dist/` にソース配布物、wheel、SHA-256一覧、版/ファイル数/サイズのマニフェストを生成する。
この操作は外部へのアップロード・タグ作成を行わない。
`scripts/build_release.py` は不要な実行データ・書庫・秘密パス/トークンの混入と同梱ファイルを検査する。
自動検査に加え公開前に差分・設定例・ライセンスをレビューする。

別の仮想環境でインストール済みwheelの導入・整理・復元・DB/情報源/履歴の引き継ぎを再検証できる。

```bash
.venv/bin/python scripts/release_smoke.py \
  --wheel dist/komga_manga_kit-0.1.0rc1-py3-none-any.whl \
  --output /tmp/manga-kit-release-smoke.json
```

手元で保管した信頼できるdev8のwheelがあれば `--previous-wheel /path/to/previous.whl` を追加する。
試験専用ディレクトリに架空22冊と仮想環境を作成し、既存の設定・APIキー・実ライブラリは使わない。
