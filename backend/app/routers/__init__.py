"""业务路由汇总：通用资源路由 + 领域动作路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import domain, resource

# 领域动作路由必须先于通用资源路由注册：通用路由含 /api/{module}/{entry_id}，
# 否则 /api/unit_calibrate/points 这类固定子路径会被动态路径抢先匹配。
ROUTERS = [domain, resource]
