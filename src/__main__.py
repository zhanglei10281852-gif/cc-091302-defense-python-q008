"""启动预约联动服务：python -m src --db data/visit.db --port 8080"""
from __future__ import annotations

import argparse

from .api import make_server
from .service import VisitService


def main() -> None:
    parser = argparse.ArgumentParser(description="基地访客预约联动服务")
    parser.add_argument("--db", default="visit_linkage.db", help="SQLite 数据文件路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    service = VisitService(db_path=args.db)
    server = make_server(service, host=args.host, port=args.port)
    print(f"预约联动服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
