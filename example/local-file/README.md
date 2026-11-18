# 本地文件安装示例

此示例把随附的普通文本安装为指定目录中的 `message.txt`，权限为 `0644`。重复运行会重新观察目标：内容和权限均匹配时复用文件，并继续执行两项独立核查。

## 准备

需要 Python 3.12 和 uv。从仓库根目录构建公共包，再更新示例锁文件并同步独立环境：

```bash
uv build --wheel
if [ -f example/local-file/uv.lock ]; then
    lock_backup=$(mktemp "${TMPDIR:-/tmp}/xxautoenv-local-file-uv.lock.XXXXXX")
    mv example/local-file/uv.lock "$lock_backup"
    printf 'Previous lock: %s\n' "$lock_backup"
fi
uv lock --project example/local-file
uv sync --locked --project example/local-file
```

首次准备可能需要联网取得构建依赖、SpiffWorkflow、Rich 和 lxml 的平台 wheel。示例依赖显式指向 `dist/xxautoenv-0.1.0-py3-none-any.whl`；缺少该文件时先构建，示例不会从索引下载 xxautoenv。重建同名 wheel 后重新执行上述备份、锁定和同步命令；直接更新已有锁文件可能保留旧 wheel 摘要。备份位置由命令输出，重新生成的锁文件记录实际 wheel 摘要。

## 执行

在示例目录启动部署。配置阶段的第一步确定配置来源。已有文件时可选择其中一份或新建，目录为空时直接新建。

```bash
cd example/local-file
.venv/bin/python deploy.py
```

`--config` 直接指定文件，配置阶段仍展示选中的路径。将下列路径换成实际保存的文件名。

```bash
.venv/bin/python deploy.py --config .xxautoenv/config/demo-20261117-143205.json -y
```

引导阶段的第一步读取、校验并补齐 `target_dir`。缺项时输入目标目录，例如 `targets/demo`。录入完成后，新配置保存在 `.xxautoenv/config/`，按目标目录名和时间命名，重名时报错。补齐已有配置时静默写回原文件。文件无法读取或已有字段无效时，引导失败并退出。非交互运行需要完整配置。

引导阶段的第二步读取随附的来源文件，确定交付所需的内容摘要、权限和目标位置。来源文件缺失或不可用时，部署在引导阶段停止。

相对配置路径、配置中的 `target_dir` 和来源材料都以 `deploy.py` 所在目录为基准。目标目录位于该目录内，可以尚未创建。`.xxautoenv/tools/` 是该部署目录内的工具缓存位置，本示例只创建配置目录。

`-y` 自动批准部署变更。交付审批覆盖写入及所需的目录创建。配置、引导、执行和最终验收按展示顺序连续编号，相邻阶段之间有一条分割线。步骤表列出 `#`、`说明`、`Allowed actions`、`Status`。后两列的框架文案使用英文。执行和最终验收的状态来自实际准备结果，如 `observed`、`ready`、`pending`。其他框架标签使用中文，消费者的步骤说明及动作标识保持原样。

首次运行交付文件。再次运行时，计划中的复用步骤显示 `no_change`，随后执行阶段按编号显示两步“完成”、两步“未选择”和一步“跳过”。两项核查均报告“完成；通过”，最后一行显示“全部成功完成”。

成功退出码为 0，失败为 1，拒绝为 2，中断为 130。符号链接或非普通目标文件会报错并停止；审批期间来源或目标发生变化也会失败。核查不通过或无法判定使整体失败，其他独立核查仍继续；普通执行失败则停止后续步骤。

## 离线准备与消费

依赖缓存准备齐全后，在仓库根目录可以使用 `uv sync --locked --offline --project example/local-file` 同步环境；这个命令仍属于准备阶段。直接执行 `.venv/bin/python` 不会同步依赖。离线缓存缺失时须先补齐依赖，部署脚本不安装工具链。

复制到其他目录时，同时复制 wheel，并在 `pyproject.toml` 的 `tool.uv.sources.xxautoenv.path` 指向实际文件，再运行 `uv lock` 和 `uv sync --locked`。示例是独立项目，未加入 uv workspace；它不需要仓库的源码目录或 `PYTHONPATH`。

修改流程时，在 `deploy.py` 中使用原生 SpiffWorkflow 连接节点；交付或复用由消费者选择。公共包架构见[架构说明](../../docs/architecture.md)，验证方式及平台限制见[验证指南](../../docs/testing.md)。
