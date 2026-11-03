"""代码审查子模块。

把 PR/工作区的 diff 解析、审查发现的结构化记录与发布拆成四个模块：

- ``diff``      本地 git diff 解析与 finding 位置校验（确定性，不调模型）。
- ``findings``  审查发现的数据结构与 Markdown 汇总。
- ``engine``    把 Store 里的 findings 汇总成一段评论文本。
- ``publish``   把 findings 发回 Gitee/GitHub/GitLab 的 PR 评论。

审查的模型编排（code_reviewer 子 Agent）不在这里，见 ``familiar/server.py``。
"""
