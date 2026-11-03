"""仓库记忆与工作区事实记忆。

三个模块：

- ``workspace``  工作区目录用途的客观描述（``load_workspace_memory``）。
- ``repo``       按 owner/repo 组织的仓库级记忆（LangGraph Store 持久化）。
- ``update``     任务完成后从文本里提炼、去重、写回仓库记忆。
"""
