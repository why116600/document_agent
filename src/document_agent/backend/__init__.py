"""文档智能体的 Web 后端（FastAPI）。

入口：
    cd src && python -m document_agent.backend

模块说明：
    bootstrap.py      把 ``src``、``agent``、``retrieve_tool`` 加入 ``sys.path``，
                      让散装模块（``llm_client`` 等）可以按控制台的方式导入；
    config.py         路径、配置、支持的文件类型等常量；
    store.py          会话文件夹（元数据、消息、文件）的读写；
    jobs.py           后台任务、事件队列与 stdout 日志捕获；
    agent_runtime.py  智能体实例管理、知识库懒加载、一次写作任务的执行；
    app.py            FastAPI 应用与 REST/SSE 接口；
    static/           前端页面（原生 HTML/CSS/JS，无需构建）。
"""

__version__ = "0.1.0"
