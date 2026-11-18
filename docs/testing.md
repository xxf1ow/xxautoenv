# 验证指南

测试使用真实 SpiffWorkflow 3.2.0 和 Rich 15.0.0，覆盖节点、基础操作、运行交互和独立 wheel 消费。按[开发指南](development.md#开发环境)准备 Python 3.12、uv、构建依赖及平台 wheel，测试执行期间不下载依赖或安装工具链。

## 定向验证

在仓库根目录按修改范围运行所属模块，例如：

```bash
uv run --locked python -m unittest tests.test_preparation tests.test_node -v
uv run --locked python -m unittest tests.test_example -v
```

示例测试用标准库 pty 驱动真实终端，覆盖新建和选择配置、缺项补录、无效配置停止及拒绝部署写入。交付路径覆盖文件复用、内容和权限变化，以及独立核查继续。测试检查四列表、阶段分割线、按计划编号呈现的结果、非通过核查原因及最后一行的全局结论。

先按[开发指南](development.md#构建与独立消费)构建 wheel，再执行：

```bash
uv run --locked python -m unittest tests.test_wheel_consumer -v
```

缺少 wheel 时测试明确要求先构建。测试检查包清单，将示例和 wheel 复制到仓库外，更新本地来源并重新生成锁文件。测试清除 `PYTHONPATH`，在全新环境离线同步并核对 site-packages 导入位置。随后交互创建 A 和 B，从另一工作目录通过 `--config` 重跑 A，验证替换、权限恢复和独立核查。另一次执行阻断 socket、连接和地址解析，以验证部署运行期可以离线。

## 阶段验证

阶段末集中运行全量测试，避免每个局部任务重复执行：

```bash
uv run --locked python -m unittest discover -s tests -v
```

在仓库根目录检查文档及空白：

```bash
python3 scripts/validate_project_docs.py
git diff --check
git diff --cached --check
```

文档验证器读取 `scripts/doc-budgets.manifest.json`，检查预算、本地链接、标题锚点及源码文档引用；错误返回非零退出码。Agent Note 修改另运行 `agent-notes` 技能随附的 `validate_agent_notes.py`。

## 平台与证据边界

macOS arm64 和断网的 Linux arm64 容器均使用 Python 3.12.16 通过全量测试。验证覆盖本地命令输出、退出及中断，文件权限、替换及审批前提，真实终端和独立 wheel 的离线消费。

本地测试不证明远端 SSH、插件 ABI、pyinfra 或 OpenTofu 可用。依赖预先准备与运行期无需外网是不同证据；选择阶段的临时探针不代替本仓库实际产物验证。后续范围见[路线图](roadmap.md)。
