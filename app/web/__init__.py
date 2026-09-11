"""内置网页监控服务。

同样不做包级 re-export：``from app.web.server import WebServer`` 即可，
避免导入本包时连带拉起 HTTP 服务栈与图标数据。
"""
