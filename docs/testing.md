# 验证指南

测试使用真实 SpiffWorkflow 3.2.0 和 Rich 15.0.0，覆盖节点、基础操作、运行交互和独立 wheel 消费。按[开发指南](development.md#开发环境)准备 Python 3.12、uv、构建依赖及平台 wheel，测试执行期间不下载依赖或安装工具链。

## 定向验证

在仓库根目录按修改范围运行所属模块，例如：

```bash
uv run --locked python -m unittest tests.test_preparation tests.test_node -v
uv run --locked python -m unittest tests.test_example -v
```

示例测试实际执行部署脚本，覆盖首次交付、重跑复用、内容及权限变化、拒绝保留文件和独立核查继续。标准库 pty 驱动真实终端输入、审批和拒绝。测试检查四列表、阶段分割线、交付和复用的精确结果行、晚确定路由的未选择项、非通过核查原因及最后一行的全局结论。

先按[开发指南](development.md#构建与独立消费)构建 wheel，再执行：

```bash
uv run --locked python -m unittest tests.test_wheel_consumer -v
```

缺少 wheel 时测试明确要求先构建。测试先检查包清单，再把示例和 wheel 复制到仓库外，更新本地来源并重新生成锁文件。它清除 `PYTHONPATH`，在全新环境离线同步，断言实际导入位于该环境的 site-packages，并执行交付和复用。额外进程内执行阻断 socket、连接与地址解析，补查运行期无需网络；`uv --offline` 本身只证明依赖准备未联网。

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

macOS 的 Python 3.12.16 已验证本地命令输出、退出及中断，文件权限、替换及审批前提，真实终端和独立 wheel 的离线消费。Linux 尚未验证；当前没有获授权的 Linux 主机或容器，不能据 macOS 结果声称跨平台验收通过。

本地测试不证明远端 SSH、插件 ABI、pyinfra 或 OpenTofu 可用。依赖预先准备与运行期无需外网是不同证据；选择阶段的临时探针不代替本仓库实际产物验证。后续范围见[路线图](roadmap.md)。
