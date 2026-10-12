import argparse
import json
from pathlib import Path
import sys

from .config import ConfigError, load_settings
from .library import LibraryService
from .organizer import plan_organization
from .sample import create_sample_library


def main():
    parser = argparse.ArgumentParser(description="日本語漫画の整理とKomga連携の基盤")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="ローカルUIを起動")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5080)
    commands.add_parser("scan", help="蔵書とメタデータを読み取り、JSONで表示")
    commands.add_parser("plan", help="整理プレビューをJSONで表示（変更なし）")
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
            result = LibraryService(config).scan()
            output = ({"series": [s.to_dict() for s in result.series], "stats": result.stats(),
                       "warnings": result.warnings, "truncated": result.truncated, "demo": config.demo}
                      if args.command == "scan" else plan_organization(result.series, config.layout,
                                                                      None if config.demo else config.library_root))
            print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ConfigError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
