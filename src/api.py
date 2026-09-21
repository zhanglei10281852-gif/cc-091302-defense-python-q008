"""HTTP JSON API（仅依赖标准库）。

路由：
  POST /plans                  创建访问计划
  GET  /plans                  计划列表（?status=ACTIVE 过滤）
  GET  /plans/{id}             管理视图：当前步骤/责任岗位/阻塞原因
  GET  /plans/{id}/timeline    到访时间线（事件 + 变更审计）
  POST /plans/{id}/results     终端提交核验结果（按事件编号幂等）
  POST /plans/{id}/changes     临时变更（必须带批准人与理由）
  POST /plans/{id}/cancel      取消计划
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .service import ServiceError, VisitService

_ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/plans$"), "create_plan"),
    ("GET", re.compile(r"^/plans$"), "list_plans"),
    ("GET", re.compile(r"^/plans/(?P<plan_id>[^/]+)$"), "get_plan"),
    ("GET", re.compile(r"^/plans/(?P<plan_id>[^/]+)/timeline$"), "get_timeline"),
    ("POST", re.compile(r"^/plans/(?P<plan_id>[^/]+)/results$"), "submit_result"),
    ("POST", re.compile(r"^/plans/(?P<plan_id>[^/]+)/changes$"), "apply_change"),
    ("POST", re.compile(r"^/plans/(?P<plan_id>[^/]+)/cancel$"), "cancel_plan"),
]


def make_server(service: VisitService, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        server_version = "VisitLinkage/1.0"

        # -------------------------------------------------- 工具
        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ServiceError("BAD_JSON", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ServiceError("BAD_JSON", "请求体必须是 JSON 对象")
            return data

        def log_message(self, fmt: str, *args: Any) -> None:  # 保持安静，审计以时间线为准
            pass

        # -------------------------------------------------- 分发
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            for m, pattern, action in _ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if match:
                    try:
                        handler: Callable[..., Any] = getattr(self, f"_{action}")
                        result = handler(**match.groupdict())
                        self._send(200, result)
                    except ServiceError as e:
                        status = 404 if e.code == "PLAN_NOT_FOUND" else 409
                        self._send(status, {"error": e.code, "message": e.message})
                    except (KeyError, ValueError) as e:
                        self._send(400, {"error": "BAD_REQUEST", "message": str(e)})
                    return
            self._send(404, {"error": "NOT_FOUND", "message": f"未知路由: {method} {path}"})

        # -------------------------------------------------- 各端点
        def _create_plan(self) -> dict:
            return service.create_plan(**self._read_json())

        def _list_plans(self) -> dict:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            return {"plans": service.list_plans(status=params.get("status"))}

        def _get_plan(self, plan_id: str) -> dict:
            view = service.get_plan_view(plan_id)
            view["timeline"] = service.get_timeline(plan_id)
            return view

        def _get_timeline(self, plan_id: str) -> dict:
            return {"plan_id": plan_id, "timeline": service.get_timeline(plan_id)}

        def _submit_result(self, plan_id: str) -> dict:
            return service.submit_result(plan_id=plan_id, **self._read_json())

        def _apply_change(self, plan_id: str) -> dict:
            return service.apply_change(plan_id=plan_id, **self._read_json())

        def _cancel_plan(self, plan_id: str) -> dict:
            return service.cancel_plan(plan_id=plan_id, **self._read_json())

    return ThreadingHTTPServer((host, port), Handler)
