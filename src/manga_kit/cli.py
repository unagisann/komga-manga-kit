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
    for name, help_text in [("execute", "確認した整理計画をバックアップ後に実行"),
                            ("restore", "整理操作を上書きせず復元"), ("operation", "保存した整理操作を表示")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id")
        if name != "operation":
            command.add_argument("--confirm", required=True, help="確認した計画のdigest")
    sample = commands.add_parser("sample", help="架空のサンプルCBZを空のフォルダに生成")
    sample.add_argument("--output", type=Path, default=Path("var/sample-library"))
    args = parser.parse_args()
    try:
        config = load_settings(args.config)
        if args.command == "serve":
            from .app import create_app
            create_app(settings=config).run(host=args.host, port=args.port)
        elif args.command == "sample":
            count = create_sample_library(args.output, config.layout)
            print(f"{args.output.resolve()} に {count} 冊のサンプルを作成しました。")
        else:
            store = StateStore(config)
            store.recover()
            library = LibraryService(config, store=store)
            if args.command in {"prepare", "execute", "restore", "operation"}:
                service = OperationService(config, store)
                if args.command == "prepare":
                    output = service.prepare(selected=args.series)
                elif args.command == "operation":
                    output = service.inspect(args.id)
                else:
                    output = getattr(service, args.command)(args.id, args.confirm)
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
