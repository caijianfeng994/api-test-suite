#!/usr/bin/env python3
"""自然语言只读查库。复制自 traffic-investment/scripts/db_agent.py。

差异：使用 `--uri` / `--config`，不绑定流量投放新旧库。
LLM：`backend.app.core.llms.get_text_model()`。
数据库连接强制 `default_transaction_read_only=on`。
严禁 DELETE / INSERT / UPDATE / DROP / ALTER / TRUNCATE / CREATE。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from db_readonly import load_uri_from_config  # noqa: E402


def _ensure_backend_import_path() -> None:
    """把仓库 `backend/` 加入 sys.path，以便 `from app.core.llms import get_text_model`。"""
    repo_root = Path(__file__).resolve().parents[4]
    backend = repo_root / "backend"
    if not backend.is_dir():
        raise SystemExit(f"未找到 backend 目录: {backend}")
    backend_str = str(backend)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)


async def run_agent(uri: str, question: str) -> None:
    _ensure_backend_import_path()
    from app.core.llms import get_text_model
    from langchain_community.agent_toolkits import SQLDatabaseToolkit
    from langchain_community.utilities import SQLDatabase
    from langgraph.prebuilt import create_react_agent

    llm = get_text_model()
    db = SQLDatabase.from_uri(
        uri,
        engine_args={
            "connect_args": {"options": "-c default_transaction_read_only=on"},
        },
    )
    tools = SQLDatabaseToolkit(db=db, llm=llm).get_tools()
    system_prompt = f"""
你是一个设计用于与 SQL 数据库交互的智能体。
给定输入问题后，创建一个语法正确的 {db.dialect} 查询，
查看结果后返回答案。默认最多返回 10 条。
永远不要查询特定表的所有列，只问相关列。
执行前必须检查 SQL；出错则重写重试。
数据库连接已强制设置为只读。
只允许 SELECT、只读 WITH、EXPLAIN、SHOW。
严禁 DELETE，并禁止 INSERT、UPDATE、DROP、ALTER、TRUNCATE、CREATE、
GRANT、REVOKE 等任何写入或结构变更；不得尝试绕过只读限制。
开始时先查看表结构，再查最相关表的 schema。
""".strip()
    agent = create_react_agent(llm, tools, prompt=system_prompt)
    async for step in agent.astream(
        {"messages": [{"role": "user", "content": question}]},
        stream_mode="values",
    ):
        step["messages"][-1].pretty_print()


def main() -> int:
    parser = argparse.ArgumentParser(description="api-test-suite 自然语言只读查库")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--uri", help="postgresql+psycopg2://...")
    src.add_argument("--config", help="db-config.json")
    parser.add_argument("--question", required=True)
    args = parser.parse_args()
    uri = args.uri or load_uri_from_config(Path(args.config))
    asyncio.run(run_agent(uri, args.question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
