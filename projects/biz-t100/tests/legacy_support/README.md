# 历史测试支持资料

本目录保留 12 份历史 Python 和 1 份历史 Shell，供本项目离线回归使用，不是日常测试入口。

日常仅使用 [offline.py](../../scripts/offline.py) 与 [live.py](../../scripts/live.py)，命令统一见 [测试说明](../../docs/testing.md)。旧模拟、常驻端口、plan/call/review 等流程只反映历史阶段，不能据此重新装配当前服务。

[origins.json](origins.json) 逐项记录原 scripts/ 路径、归档路径、原始 SHA-256 和迁移适配。Python 的自动 main 启动段已移除，必要 main/run 辅助函数由离线回归显式调用；项目根路径因目录深度调整。Shell 原文保留为 .sh.txt 且没有执行权限。产品 src/ 不导入本目录。

原运行数据、原接口文档、历史验证制品及 unknown 事实没有迁移、删除或改写。回归测试捕获业务 HTTP，不执行真实 T100 写入。维护本目录时不得恢复日常入口；新增日常需求在现有两个脚本内表达。
