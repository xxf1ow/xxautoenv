# 验证指南

节点测试覆盖原生 SpiffWorkflow 流程中的输入解析、动作审批绑定、结果传递和异常停止。文档检查覆盖结构、链接、篇幅和空白错误。

## 节点测试

按[开发指南](development.md#开发环境)准备依赖后，在仓库根目录运行：

```bash
uv run --locked python -m unittest tests.test_node -v
```

测试使用真实 SpiffWorkflow 3.2.0 和记录型运行交互，不执行外部部署。按修改范围选择所属模块测试；阶段验收时集中运行全量测试。

## 文档检查

在仓库根目录使用开发环境的 Python 运行；检查脚本只依赖标准库和 Git：

```bash
uv run --locked python scripts/validate_project_docs.py
```

检查工作区和暂存区的空白错误：

```bash
git diff --check
git diff --cached --check
```

检查脚本来自 `doc-standards` 技能的文档验证器，初始化时原样引入。它读取 `scripts/doc-budgets.manifest.json`，检查文档预算、本地链接、标题锚点及源码中的文档引用；错误时返回非零退出码。

## 产品验证边界

节点测试不代表独立 wheel 消费、完整部署交互或跨平台验收已经通过。第一阶段的验证目标见[路线图](roadmap.md)。临时探针不是产品验收证据，也不替代对正式发布产物的验证。

对离线能力的声明必须区分预先准备依赖与实际运行；对远端能力的声明必须有对应环境中的证据，不能由本地文件替身推导。
