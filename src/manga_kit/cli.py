import argparse
import json
from pathlib import Path
import sys

from .config import ConfigError, load_settings
from .library import LibraryService
from .jobs import JobService
from .organizer import plan_organization
from .sample import create_sample_library
from .state import StateStore
from .state import StateConflict
from .komga import KomgaError
from .operations import OperationService
from .conversion import ConversionService
from .normalization import NormalizationOperations
from .archives import PAGE_ORDERS
from .komga_metadata import FIELD_MAP, KomgaMetadataService
from .komga_authors import KomgaAuthorsService
from .authors import author_candidates
from .komga_batch import MODES, KomgaBatchService, batch_summary
from .komga_import import KomgaReadingImportService


def main():
    parser = argparse.ArgumentParser(description="日本語漫画の整理とKomga連携の基盤")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="ローカルUIを起動")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5080)
    for name, help_text in [("scan", "蔵書とメタデータを読み取り、JSONで表示"),
                            ("plan", "整理プレビューをJSONで表示（変更なし）")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--record", action="store_true", help="処理履歴と結果を保存")
    jobs = commands.add_parser("jobs", help="保存した処理履歴をJSONで表示")
    jobs.add_argument("--limit", type=int, default=50)
    job = commands.add_parser("job", help="保存した処理の結果を全件表示")
    job.add_argument("id")
    prepare = commands.add_parser("prepare", help="整理の実行計画と変更前情報を保存")
    prepare.add_argument("--series", action="append", help="対象の作品ID（複数指定可能）")
    conversion = commands.add_parser("prepare-conversion", help="元書庫を保持するCBZ生成計画とページ順を確認")
    conversion.add_argument("--book", action="append", required=True, help="元書庫の相対パス（複数指定可能）")
    conversion.add_argument("--output", type=Path, required=True, help="別の変換先フォルダ（事前に作成）")
    conversion.add_argument("--page-order", choices=PAGE_ORDERS, default="natural-path",
                            help="ページ順。既存KomgaToolと同じ辞書順はlexicographic-path")
    metadata = commands.add_parser("prepare-metadata", help="Komga作品情報の項目別反映計画と変更前情報を保存")
    metadata.add_argument("--series", required=True, help="対象のローカル作品ID")
    metadata.add_argument("--komga-series", help="Komga作品ID（省略時はパス対応から特定）")
    metadata.add_argument("--field", action="append", required=True, choices=FIELD_MAP, help="反映する項目（複数指定可能）")
    metadata.add_argument("--replace-locked", action="store_true", help="選んだロック済み項目の変更を計画に含める")
    metadata.add_argument("--replace-lists", action="store_true", help="タグ・ジャンルを追加方式から一覧の置き換えにする")
    completion = commands.add_parser("prepare-completion", help="手動で完結にした作品のKomgaタグ・連載状態の反映計画")
    completion.add_argument("--series", required=True, help="完結にしたローカル作品ID")
    completion.add_argument("--komga-series", help="Komga作品ID（省略時はパス対応から特定）")
    completion.add_argument("--ongoing-tag", default="連載中", help="取り除く連載中タグ（既定: 連載中）")
    completion.add_argument("--completed-tag", default="完結", help="追加する完結タグ（既定: 完結）")
    completion.add_argument("--replace-locked", action="store_true", help="ロック済みの値の変更を計画に含める")
    candidates = commands.add_parser("author-candidates", help="ローカルの著者表記から確認用の名前・役割を表示（書き込みなし）")
    candidates.add_argument("--series", required=True)
    authors = commands.add_parser("prepare-authors", help="巻ごとのKomga著者・役割の反映計画")
    authors.add_argument("--series", required=True)
    authors.add_argument("--komga-series", help="Komga作品ID（省略時はパス対応から特定）")
    source = authors.add_mutually_exclusive_group(required=True)
    source.add_argument("--author", nargs=2, action="append", metavar=("ROLE", "NAME"), help="確認した役割と著者名（複数指定可）")
    source.add_argument("--clear-authors", action="store_true", help="空一覧へ置き換える（--replace-lists 必須）")
    authors.add_argument("--book", action="append", help="作品内の巻の相対パス。省略時は全巻、最大50巻")
    authors.add_argument("--replace-lists", action="store_true")
    authors.add_argument("--replace-locked", action="store_true")
    batch = commands.add_parser("prepare-komga-batch", help="複数作品のKomga作品情報・完結・著者の反映計画（書庫は属性で照合）")
    batch.add_argument("--mode", required=True, choices=MODES, help="metadata（作品情報）/ completion（完結）/ authors（巻の著者）")
    batch_scope = batch.add_mutually_exclusive_group(required=True)
    batch_scope.add_argument("--series", action="append", help="対象の作品ID（複数指定可能）")
    batch_scope.add_argument("--all", action="store_true", help="ライブラリの全作品を対象にする")
    batch.add_argument("--field", action="append", choices=FIELD_MAP, help="metadata で反映する項目（複数指定可能）")
    batch.add_argument("--overwrite", action="store_true", help="metadata でKomgaに既にある値も置き換える（既定は空欄だけ）")
    batch.add_argument("--replace-locked", action="store_true", help="ロック済みの項目の変更を計画に含める")
    batch.add_argument("--replace-lists", action="store_true", help="タグ・ジャンル・著者を追加ではなく置き換える")
    batch.add_argument("--ongoing-tag", default="連載中", help="completion で取り除く連載中タグ")
    batch.add_argument("--completed-tag", default="完結", help="completion で追加する完結タグ")
    readings = commands.add_parser("prepare-reading-import", help="Komgaの並び順（titleSort）から未設定の読みを取り込む計画")
    reading_scope = readings.add_mutually_exclusive_group(required=True)
    reading_scope.add_argument("--series", action="append", help="対象の作品ID（複数指定可能）")
    reading_scope.add_argument("--all", action="store_true", help="ライブラリの全作品を対象にする")
    for name, help_text in [("execute", "確認した操作計画を実行（整理・CBZ生成・Komga作品情報）"),
                            ("restore", "保存した操作を復元・生成を取り消し"), ("operation", "保存した操作と計画を表示")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id")
        if name != "operation":
            command.add_argument("--confirm", required=True, help="確認した計画のdigest")
    sample = commands.add_parser("sample", help="架空のサンプルCBZを空のフォルダに生成")
    sample.add_argument("--output", type=Path, default=Path("var/sample-library"))
    single = commands.add_parser("convert-one", help="Windows等で1冊の書庫を元のまま保持してCBZ化")
    single.add_argument("source", type=Path)
    single.add_argument("--output", required=True, type=Path)
    single.add_argument("--page-order", choices=PAGE_ORDERS, default="natural-path")
    single.add_argument("--confirm", help="事前に表示したdigest")
    args = parser.parse_args()
    try:
        config = load_settings(args.config)
        if args.command == "convert-one":
            from .convert_one import convert_one
            output = convert_one(args.source, args.output, page_order=args.page_order, enabled=config.execution_enabled, confirm=args.confirm)
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            from .app import create_app
            create_app(settings=config).run(host=args.host, port=args.port)
        elif args.command == "sample":
            count = create_sample_library(args.output, config.layout)
            print(f"{args.output.resolve()} に {count} 冊のサンプルを作成しました。")
        else:
            store = StateStore(config)
            store.recover()
            library = LibraryService(config, store=store)
            if args.command == "author-candidates":
                series = next((entry for entry in library.scan().series if entry.id == args.series), None)
                if series is None:
                    raise ConfigError("対象のローカル作品が見つかりません。")
                output = {"series_id": series.id, "source_text": series.author,
                          "candidates": author_candidates(series.author), "review_required": True}
            elif args.command in {"prepare", "prepare-conversion", "prepare-metadata", "prepare-completion", "prepare-authors",
                                  "prepare-komga-batch", "prepare-reading-import", "execute", "restore", "operation"}:
                service = OperationService(config, store)
                if args.command == "prepare":
                    output = service.prepare(selected=args.series)
                elif args.command == "prepare-conversion":
                    output = ConversionService(config, store).prepare(args.book, args.output, page_order=args.page_order)
                elif args.command == "prepare-metadata":
                    output = KomgaMetadataService(config, store).prepare(args.series, args.komga_series, args.field,
                        replace_locked=args.replace_locked, replace_lists=args.replace_lists)
                elif args.command == "prepare-completion":
                    output = KomgaMetadataService(config, store).prepare_completion(args.series, args.komga_series,
                        ongoing_tag=args.ongoing_tag, completed_tag=args.completed_tag, replace_locked=args.replace_locked)
                elif args.command == "prepare-authors":
                    values = [{"role": role, "name": name} for role, name in (args.author or [])]
                    output = KomgaAuthorsService(config, store).prepare(args.series, args.komga_series, values,
                        selected=args.book, replace_lists=args.replace_lists, replace_locked=args.replace_locked)
                elif args.command == "prepare-komga-batch":
                    output = batch_summary(KomgaBatchService(config, store).prepare(
                        args.mode, None if args.all else args.series, fields=args.field, overwrite=args.overwrite,
                        replace_locked=args.replace_locked, replace_lists=args.replace_lists,
                        ongoing_tag=args.ongoing_tag, completed_tag=args.completed_tag))
                elif args.command == "prepare-reading-import":
                    output = batch_summary(KomgaReadingImportService(config, store).prepare(None if args.all else args.series))
                elif args.command == "operation":
                    output = service.inspect(args.id)
                else:
                    kind = service.inspect(args.id)["plan"].get("kind")
                    if kind == "conversion":
                        service = ConversionService(config, store)
                    elif kind == "normalize":
                        service = NormalizationOperations(config, store)
                    elif kind == "komga_metadata":
                        service = KomgaMetadataService(config, store)
                    elif kind == "komga_authors":
                        service = KomgaAuthorsService(config, store)
                    elif kind == "komga_batch":
                        service = KomgaBatchService(config, store)
                    elif kind == "komga_reading_import":
                        service = KomgaReadingImportService(config, store)
                    output = getattr(service, args.command)(args.id, args.confirm)
                    if kind in {"komga_batch", "komga_reading_import"}:
                        # Per-file signatures for a whole library are not useful terminal output.
                        output = batch_summary(output)
            elif args.command == "jobs":
                if not 1 <= args.limit <= 200:
                    raise ConfigError("limit は1〜200で指定してください。")
                output = {"jobs": store.jobs(args.limit)}
            elif args.command == "job":
                output = store.job(args.id, include_result=True)
                if output is None:
                    raise ConfigError("処理が見つかりません。")
            elif args.record:
                service = JobService(library, store)
                try:
                    job_id = service.submit("scan" if args.command == "scan" else "preview")
                finally:
                    service.shutdown()
                output = store.job(job_id, include_result=True)
                if output["status"] != "succeeded":
                    raise ConfigError(output["error"])
            else:
                result = library.scan()
                output = ({"series": [s.to_dict() for s in result.series], "stats": result.stats(),
                           "warnings": result.warnings, "truncated": result.truncated, "demo": config.demo}
                          if args.command == "scan" else plan_organization(result.series, config.layout,
                                                                          None if config.demo else config.library_root))
            print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ConfigError, StateConflict, KomgaError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
