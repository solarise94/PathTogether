"""plugins 包根标记（Stage 5-1）。

仅 plugins/ 与 plugins/sdk/ 加 __init__.py，使 app.py 可
``from plugins.sdk.manifest import ...``。**plugins/histopilot/ 下不加**
__init__.py，以免影响其作为静态资源目录的服务方式。
"""
