# CBZ生成とページ順の正規化

0.1.0.dev3は元書庫を保持し、別の保存先へCBZを生成する。
内容ハッシュが変わるため、Komga管理下の書庫を自動で置き換える機能・変換先への既読移行は未実装。
元書庫と元の既読情報を保持したまま、生成結果を別ライブラリで確認する。

## 対応範囲

| 項目 | 動作 |
| --- | --- |
| ZIP / CBZ | Python標準ライブラリで読み取り、CBZへ正規化 |
| RAR / CBR / 7z | libarchiveで読み取り、CBZへ生成 |
| ページ順 | 書庫内のフルパスを自然順に並べ、数字部分を数値として扱う |
| ページ名 | `0001.png`、`0002.jpg` など。拡張子を小文字へ揃える |
| 画像 | 内容のバイト列をそのままコピー。再エンコードしない |
| ComicInfo.xml | 書庫ルートへ置き、内容をバイト単位で保持する |
| その他のファイル | 書庫内の元の名前・内容を保持する |
| ZIPコメント | 元のZIP/CBZのコメントを保持する |
| 元書庫 | 移動・削除・上書きせず保持する |
| 取り消し | 自分が生成した、変更されていないCBZだけを削除する |

自然順の例: `chapter2/page1.png` → `chapter2/page2.png` → `chapter2/page10.png` → `chapter10/page1.png`。
連番化前に計画の `archive.members` で元と先の名前・ページ番号を確認する。
元のZIP格納順は読み順として使わない。同じ数値の表記は元の名前で決定的に並べる。
ComicInfoの `Pages/Page` の `Image` はこの自然順の0始まりの番号として扱う。
ページ数が一致しない、参照番号が範囲外/重複、XMLが不正な場合は変更せず拒否する。
ComicInfoのページ参照を別の並びへ書き換える機能は未実装。

ページとして扱う拡張子はJPG/JPEG、PNG、GIF、BMP、WebP、AVIF、JXL、HEIC/HEIF、TIF/TIFF。
読み手側の形式対応や画像の正常性は別途確認する。実機ではPNGの順序・内容を検証している。
ComicInfo.xmlが複数ある書庫、隠し画像、リンク・特殊ファイル、絶対パス・階層逸脱、同名/表記違いの項目、
暗号化・分割書庫、自己展開RAR、EPUBは対応範囲外。安全に読み取れない圧縮方式は元書庫を保持してエラーにする。

1書庫あたり10,000項目、1ファイル256MiB、展開合計4GiB、ComicInfo.xmlは256KiBまで。
ZIPの中央ディレクトリは16MiBまで。項目一覧をメモリへ読み込む前にサイズを検証する。
空のディレクトリエントリ、書庫内のタイムスタンプ・権限・拡張属性は引き継がない。
出力はZIPの非圧縮格納（stored）。すでに圧縮された画像の再圧縮を避けるため、入力より大きくなる場合がある。

## 導入

Pythonの依存関係は通常どおり `pip install -e .` で入る。
RAR/7zを読む場合はlibarchive共有ライブラリも必要。Debian系は次で導入できる。

```bash
sudo apt-get install libarchive-dev
```

Dockerイメージにはlibarchiveを同梱する。ZIP/CBZだけならシステムのlibarchiveは不要。
macOS等の環境では [libarchive-cの導入手順](https://github.com/Changaco/python-libarchive-c#installation) を参照する。
CLI実行はPOSIXに限定する。Web画面からの生成は未実装。

## 計画・実行・取り消し

`app.demo: false`、`execution.enabled: true`、元ライブラリと別の `data.root` を設定する。
元書庫は読み取り可能でよい。変換先と実行データには書き込み権限が必要。
出力先を事前に作り、元ライブラリ・実行データとの包含関係を持たない場所にする。

```bash
mkdir -p /path/to/converted-library
.venv/bin/manga-kit --config /path/to/execute.yaml scan
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-conversion \
  --book '作品/作品 第1巻.rar' \
  --book '作品/作品 第2巻.7z' \
  --output /path/to/converted-library
```

元書庫のパスは `library.root` 基準。出力はフォルダと書庫名を保ち、拡張子を `.cbz` へ変える。
同じ出力名になる複数の書庫や、既存の出力ファイルがある場合は拒否する。
`plan.entries[].archive.members` のページ対応、メタデータ、補助ファイルと、保存先を確認する。
確認した計画の `plan.id` と `digest` を使って実行する。

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

元書庫のバックアップ → 全CBZの生成 → CRC/全項目SHA-256の検証 → 別保存先への配置の順で進む。
元と先の差し替え・衝突を確認し、公開するファイルのinodeを操作記録へ確定してから配置する。
実行中は元書庫と変換先を他のツールで変更しない。途中の失敗・停止は同じ操作IDの `restore` で取り消す。
生成後に編集・差し替えされたCBZがある場合、全体の取り消しを開始する前に拒否する。
元書庫を後から編集した場合も、その内容には手を加えない。出力先の空フォルダは保持する。

バックアップ・操作記録・一時展開データは `data.root/operations` に置く。
元書庫のコピー、全生成CBZ、一度に展開する1書庫、出力CBZの容量を確認する。
生成の取り消し後もバックアップは保持する。強制終了で残った作業ディレクトリは操作が終了した後に確認する。
ComicInfo.xmlへこのツールの手動編集を埋め込む機能、series.jsonのコピーは未実装。

## Komgaとの関係

CBZ生成・取り消しはKomgaへ通信しない。元書庫のパス・内容・ID・読書状態を保持する。
生成物を別のKomgaライブラリへ登録すると、新しい未読の書籍として扱われる。
別ライブラリへの登録・スキャンは利用者が明示的に行う。生成先を元ライブラリの中へ置くことは拒否する。
この段階では、生成物を元書庫の代わりに配置する運用と既読移行を検証範囲に含めない。
検証の具体的な範囲は [検証記録](VALIDATION.md) を参照する。
