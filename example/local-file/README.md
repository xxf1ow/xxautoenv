# 本地文件安装示例

此示例把随附的普通文本安装为指定目录中的 `message.txt`，权限为 `0644`。重复运行会重新观察目标：内容和权限均匹配时复用文件，并继续执行两项独立核查。

## 准备

需要 Python 3.12 和 uv。从仓库根目录构建公共包，再更新示例锁文件并同步独立环境：

```bash
uv build --wheel
if [ -f example/local-file/uv.lock ]; then
    lock_backup=$(mktemp /private/tmp/xxautoenv-local-file-uv.lock.XXXXXX)
    mv example/local-file/uv.lock "$lock_backup"
    printf 'Previous lock: %s\n' "$lock_backup"
fi
uv lock --project example/local-file
uv sync --locked --project example/local-file
```

首次准备可能需要联网取得构建依赖、SpiffWorkflow 和 lxml 的平台 wheel。示例依赖显式指向 `dist/xxautoenv-0.1.0-py3-none-any.whl`；缺少该文件时先构建，示例不会从索引下载 xxautoenv。重建同名 wheel 后重新执行上述备份、锁定和同步命令；直接更新已有锁文件可能保留旧 wheel 摘要。备份位置由命令输出，重新生成的锁文件记录实际 wheel 摘要。

## 执行

在示例目录运行已同步的 Python，执行期间不需要 uv 同步或外网：

```bash
cd example/local-file
.venv/bin/python deploy.py --target-dir /private/tmp/xxautoenv-local-file-demo -y
```

`-y` 自动批准变更。去掉它可逐步确认写入；拒绝返回退出码 2，并保留原有文件。目标目录未提供时通过终端询问，`-y` 不会代答输入：

```bash
.venv/bin/python deploy.py
```

来源相对 `deploy.py` 定位。目标目录可以不存在，创建目录包含在同一次待批准动作中。终端显示 guidance、execution、acceptance 三个阶段和步骤结果。首次运行交付文件；再次运行显示 `Reuse file` 跳过，并分别报告 `Content check` 与 `Permission check` 的通过结论。

成功退出码为 0，失败为 1，拒绝为 2，中断为 130。符号链接或非普通目标文件会报错并停止；审批期间来源或目标发生变化也会失败。核查不通过或无法判定使整体失败，其他独立核查仍继续；普通执行失败则停止后续步骤。

## 离线准备与消费

依赖缓存准备齐全后，在仓库根目录可以使用 `uv sync --locked --offline --project example/local-file` 同步环境；这个命令仍属于准备阶段。直接执行 `.venv/bin/python` 不会同步依赖。离线缓存缺失时须先补齐依赖，部署脚本不安装工具链。

复制到其他目录时，同时复制 wheel，并在 `pyproject.toml` 的 `tool.uv.sources.xxautoenv.path` 指向实际文件，再运行 `uv lock` 和 `uv sync --locked`。示例是独立项目，未加入 uv workspace；它不需要仓库的源码目录或 `PYTHONPATH`。

修改流程时，在 `deploy.py` 中使用原生 SpiffWorkflow 连接节点；交付或复用由消费者选择。公共包架构见[架构说明](../../docs/architecture.md)，验证方式及平台限制见[验证指南](../../docs/testing.md)。
