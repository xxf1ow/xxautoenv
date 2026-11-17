# 开发指南

本仓库面向公共部署能力的维护者。公共 Python 包位于 `src/xxautoenv/`，使用 Python 3.12、uv 和 setuptools。

架构决策见[公共部署节点与交互运行入口](agent-notes/implemented/architecture/2026-11-16-public-deployment-library.md)。

## 项目范围

公共包负责通用节点和一致的运行交互；消费仓库负责产品策略、节点组合与验收规则。公开示例使用普通本地材料，不包含真实部署信息。

## 文档归属

| 文档 | 负责内容 |
|---|---|
| docs/development.md | 开发流程和文档归属 |
| docs/testing.md | 可执行验证命令及证据边界 |
| docs/roadmap.md | 阶段目标及后续范围 |
| docs/architecture.md | 当前组成、运行过程与模块依赖 |
| docs/agent-notes/ | 正式设计、长期决策理由及生命周期 |
| .superpowers/ | 不提交的交接材料、计划和临时探针 |

设计提案与当前实现分开维护。正式设计应完整记录条件、例外和错误语义，不能用讨论摘要替代接口约定。实现改变后更新对应事实所有者，不在多个文档中重复维护同一规则。

## 工作流程

先评审书面设计，再确认实施计划和执行方式。使用普通任务分支，初始设计作为该分支首提交；实现中的设计修订留在原分支。所有提交执行 `pre-commit-checks` 技能；远端发布和推送需要明确授权。

文档使用相对 Markdown 链接，一段正文占一个物理行。文档预算在 `scripts/doc-budgets.manifest.json` 中维护，预算是上限而非缩写目标。

## 开发环境

安装 uv 后，在仓库根目录同步锁定的依赖和本地包：

```bash
uv sync --locked
```

`.python-version` 选择 Python 3.12，`uv.lock` 锁定 SpiffWorkflow 3.2.0。修改依赖声明后先运行 `uv lock`，再运行 `uv sync --locked`。首次准备环境需要取得 Python 和依赖；准备完成后的节点测试无需联网。

## 构建与独立消费

在仓库根目录构建 wheel：

```bash
uv build --wheel
```

产物为 `dist/xxautoenv-0.1.0-py3-none-any.whl`。构建依赖准备齐全时可设置 `UV_OFFLINE=1`；wheel 只包含公共包、dist-info 与许可元数据。构建后按[示例说明](../example/local-file/README.md)更新真实锁文件并同步独立消费环境，不能以开发环境的 editable 安装代替消费验证。

验证命令统一由[验证指南](testing.md)维护。
