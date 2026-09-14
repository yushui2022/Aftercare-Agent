# 本地开发、测试与打包

本文只提供已经具备的 Python 证据适配层入口，不是 API/Worker/Compose 启动指南。当前任务状态和验证记录统一在[状态台账](project-status.md)，后续实现按[工程执行计划](engineering-plan.md)推进。

## 1. 环境与依赖来源

- Python：常规 CPython 3.13.15，固定在 .python-version；包声明支持 >=3.13.15,<3.14。其他补丁或平台需要另行验证。
- 环境工具：uv >=0.9.26；本次实际使用 0.9.26。还需要 Git 和首次安装时访问 PyPI、GitHub 的网络。
- Aftercare：开发版本 0.1.0a0，没有发布到 PyPI，也没有选择项目许可证。
- EGM：0.6.0，直接依赖固定完整提交 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd，只启用 postgres extra。

依赖声明见 [pyproject.toml](../pyproject.toml)，精确解析结果见 [uv.lock](../uv.lock)。不需要并排克隆 EGM；默认安装不能引用相邻工作区的未提交内容。普通 pip 安装单独的 wheel 不会自动使用 uv.lock，不能据此声称传递依赖完全相同。

运行测试不需要模型 API Key、退款凭证、数据库服务或 Docker。测试使用合成回执和临时 SQLite；EGM PostgreSQL 模块可导入不等于已经测试 PostgreSQL 的事务与并发。

## 2. 本机 Windows 安装

以下命令从仓库根目录运行，按 PowerShell 7 验证（本次为 7.6.5）。路径是这台开发机的空间约定，不是业务代码的硬编码前提；其他机器可把缓存、解释器和临时目录改到合适的数据盘。

```powershell
$env:UV_CACHE_DIR = 'G:\DevCache\uv'
$env:UV_PYTHON_INSTALL_DIR = 'G:\DevCache\Python'
$env:UV_PYTHON_BIN_DIR = 'G:\DevCache\PythonBin'
$env:TMP = 'G:\DevCache\Temp\aftercare-a001'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null
uv python install 3.13.15 --no-bin --no-registry
uv sync --locked
```

uv 打包的 Python 下载索引滞后于 CPython 发布节奏：0.9.26 的内置索引最高只到 3.13.11，
而本项目固定 3.13.15，因此裸 `uv python install 3.13.15` 会报找不到该补丁版本。把解释器
安装命令替换为显式元数据源即可：

```powershell
uv python install 3.13.15 --no-bin --no-registry --python-downloads-json-url https://raw.githubusercontent.com/astral-sh/uv/dbda4fbf33602f64b32a5b6873d730c5afb838fa/crates/uv-python/download-metadata.json
```

该 URL 固定到具体提交而不是 `main`：元数据文件本身决定下载地址，不应随时间漂移。CI 用
环境变量形式 `UV_PYTHON_DOWNLOADS_JSON_URL` 指向同一份固定元数据（见
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)），因此 CI 与本地解析到的是同一
解释器版本；改这里时两处要一起改。该文件由上游约每周同步一次，本项目固定补丁版本，
不需要跟随更新。

不要因此降级到电脑上任意一个 Python，也不要向其他软件的系统环境安装项目依赖。上述命令不向 Windows 注册解释器或创建公共 Python 命令；项目 .venv 位于本仓库。首次下载需要网络，安装后回归本身不访问模型或业务网络。

Linux 是后续容器集成目标，本次 A0-01 尚未进行 Linux 验收。届时同样使用固定 Python 和 uv.lock，去掉 Windows 专用安装参数，并通过实际 CI/容器运行补充验证。

## 3. 每次提交前的质量检查

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
```

逐条检查退出码；安装或前一项失败时不要继续宣布全部通过。更改依赖时才显式运行 uv lock 并评审锁文件差异；日常执行使用 --locked，不能用跳过时效检查的 --frozen 掩盖元数据漂移。[uv 同步语义](https://docs.astral.sh/uv/concepts/projects/sync/)

检查范围：

- Ruff：Python、类型存根和 pyproject 配置，不修改历史文章中的示例代码。
- mypy：aftercare_agent 和 tests 的严格检查。follow_imports=silent 只限制外部依赖诊断；不是关闭本项目类型检查。
- EGM 0.6 部分返回值没有注解；适配器只在此边界使用 TypedDict 和 cast，测试验证响应信封。cast 不进行运行时校验，也不提供新的权限保证；后续返回协议变更必须同步验证。
- [适配器回归](../tests/test_evidence_adapter.py)：成功/失败回执、固定声明、角色与租户隔离、重放及响应信封。
- [安装包契约](../tests/test_package_contract.py)：固定 Git 来源、包元数据、py.typed、内置 aftercare schema 和 PostgreSQL 迁移资源；不连接数据库。
- tests/contracts：运行对象、身份/幂等、租约前置条件、等待候选、事件顺序、检查点/工具参数和调查证据正反例；这些是纯规则，不证明事务原子性或多进程恢复。契约定义见[运行时](contracts/runtime-v1.md)与[调查证据](contracts/investigation-v1.md)。

## 4. 构建 sdist，再由 sdist 构建 wheel

```powershell
uv build --sdist --out-dir dist
uv build --wheel --out-dir dist dist/aftercare_agent-0.1.0a0.tar.gz
```

产物写入项目 dist/，不提交到 Git。版本变化时同步文件名；不要只打 wheel 而漏验 sdist 中的源文件。构建依赖和 uv 构建约束均已固定在 pyproject 中；这保证所选构建工具版本，不意味着已证明跨机器逐字节相同的构建。

Aftercare wheel 只需包含适配器包、类型标记与 dist-info；EGM 的 schema/SQL 必须在 EGM 自己的安装包中存在。README 会进入包元数据，所以改完 README 后应重新构建最终验收产物。

domain 现在也属于 Aftercare 包。MANIFEST.in 显式将嵌套的 tests/contracts、uv.lock 和
.python-version 放入 sdist，测试不进入运行 wheel。验收时既检查包可导入，也检查源码
分发中的完整测试是否存在，避免只在 Git 工作区才能运行新增测试。

若 Windows 报解释器/扩展 DLL 的 WinError 32，应先停止并行环境同步并检查占用，不
关闭安全软件或修改其他项目。A0-02 的默认隔离构建曾被文件占用中断；当时使用临时
独立环境，显式安装并核验 pyproject 中的 setuptools/wheel/packaging 固定版本后，以
uv build --no-build-isolation --python <该环境解释器> 完成 sdist→wheel。该替代路径需
记录工具版本与验收结果，不能在任意系统环境中加 --no-build-isolation 就称为可复现。
当时最终普通项目环境的类型检查和回归也已重跑，实际记录见[状态台账](project-status.md)。

## 5. 排除缓存、editable 与源码目录遮蔽

在新的 PowerShell 会话中，从仓库根目录执行下列步骤。这是发布前的安装验证，不是日常每次运行所必需。临时目录使用唯一名称；不要对已有用户目录使用 pytest --basetemp。

```powershell
$qaRoot = Join-Path 'G:\DevCache\Temp' ('aftercare-package-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qaRoot | Out-Null
$env:UV_CACHE_DIR = Join-Path $qaRoot 'cache'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $qaRoot 'venv'
$env:UV_PYTHON_INSTALL_DIR = 'G:\DevCache\Python'
$env:TMP = $qaRoot
$env:TEMP = $qaRoot
uv sync --locked --no-editable
if ($LASTEXITCODE -ne 0) { throw 'Locked installation failed' }

$qaPython = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
$qaWheel = (Resolve-Path 'dist/aftercare_agent-0.1.0a0-py3-none-any.whl').Path
uv pip install --python $qaPython --no-deps --reinstall $qaWheel
if ($LASTEXITCODE -ne 0) { throw 'Wheel installation failed' }
uv pip check --python $qaPython
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }

Copy-Item -LiteralPath 'tests' -Destination (Join-Path $qaRoot 'tests') -Recurse
Push-Location $qaRoot
try {
    & $qaPython -I -c 'import pathlib, sysconfig, aftercare_agent, evidence_gated_memory; base = pathlib.Path(sysconfig.get_path("purelib")).resolve(); paths = [pathlib.Path(m.__file__).resolve() for m in (aftercare_agent, evidence_gated_memory)]; print(*paths, sep="\n"); assert all(p.is_relative_to(base) for p in paths)'
    if ($LASTEXITCODE -ne 0) { throw 'Import isolation failed' }
    & $qaPython -I -m pytest --import-mode=importlib tests -q --basetemp (Join-Path $qaRoot 'pytest-tmp')
    if ($LASTEXITCODE -ne 0) { throw 'Installed-package tests failed' }
} finally {
    Pop-Location
}
```

这里先从空缓存按 uv.lock 安装，再以 --no-deps 替换 Aftercare wheel，因此不会在 wheel 验收时重新解析一套宽松传递依赖。仓库外运行、-I、实际导入路径与 Git 来源记录一起核对，避免只看版本字符串。该过程保留临时材料用于核查；结束这个会话后回到普通开发环境，避免继续使用 UV_PROJECT_ENVIRONMENT 指向的验收环境。

EGM 目前以 Git 来源安装，因此来源测试要求 direct_url.json 中存在该 Git 提交。将来采用内部预构建 EGM wheel 时，必须另行设计并核验构建来源/摘要链，不能简单删除来源断言让测试变绿。若要联调相邻 EGM 工作区，使用单独环境并记录差异，不复用这个“固定提交验收”结果。

## 6. 仍然没有的入口

真实业务 Worker、SSE、模型和沙箱仍未交付。A1-04/A2 已提供仅供本地开发的 [Compose smoke 环境](../deploy/compose/README.md)、常驻轮询、租约心跳和 Outbox publisher；Compose 仍只启动 PostgreSQL/API，不能代替生产部署、安全审计、依赖漏洞扫描和性能验收。现有 SQLite 回归也不能代替真实 PostgreSQL 租约、事务和故障恢复测试。
