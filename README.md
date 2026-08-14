# Automatic Information Filter

[English](README.en.md)

一个可插拔的自动资讯过滤框架。

它只规定一条流水线：

```text
数据源 → 标准化 → 过滤 / 处理 → 输出
```

数据从哪里来、怎么判断、最后放到哪里，都由适配器决定。你可以接入订阅源、普通 API、网页抓取器、消息流或自己的数据库；用关键词、正则、传统算法、本地程序或远程服务做筛选；再把结果写入文件、数据库、Web 服务、邮件、IM 或知识库。

项目不绑定特定数据源、模型服务或展示平台。

## 适合做什么

- 聚合多个来源，只保留真正需要看的内容
- 给资讯打标签、评分、摘要或分类
- 把清洗后的结果交给另一个系统展示
- 用同一套流水线替换不同的数据源、过滤器和输出端
- 通过 cron、容器或工作流平台定时运行

## 当前内置组件

| 环节 | 内置实现 |
|---|---|
| 数据源 | JSON / JSONL 文件、JSON HTTP API、RSS / Atom |
| 过滤与处理 | 关键词、正则、最小长度、通用 HTTP 决策接口 |
| 输出 | JSONL、SQLite、标准输出、HTTP 接口 |
| 状态 | 内存、SQLite 持久化去重 |

内置组件只是开箱即用的起点。任何实现了 `Source`、`Processor`、`Sink` 或 `StateStore` 接口的 Python 类都可以通过配置加载。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/SiMonCcai/automatic-information-filter.git
cd automatic-information-filter
python -m venv .venv
. .venv/bin/activate
pip install -e .

aif validate -c examples/pipeline.toml
aif run -c examples/pipeline.toml
```

示例会读取一份虚构数据，执行长度和关键词过滤，并将结果写入：

```text
examples/output/accepted.jsonl
```

再次运行时，SQLite 状态库会跳过已经处理过的项目。

## 配置一条流水线

配置使用 TOML。下面这条流水线从一个 JSON API 收集数据，经两层本地规则过滤后发送给任意 HTTP 接收端：

```toml
[[sources]]
type = "http_json"
url = "https://api.example.com/items"
items_path = "data.items"
source = "example-api"
token_env = "SOURCE_API_TOKEN"

[[processors]]
type = "minimum_length"
field = "content"
minimum = 100

[[processors]]
type = "keyword"
include_any = ["AI", "automation"]
exclude_any = ["sponsored"]
fields = ["title", "content"]

[[sinks]]
type = "http"
url = "https://receiver.example.com/inbox"
token_env = "OUTPUT_API_TOKEN"

[state]
type = "sqlite"
path = ".aif/state.db"
```

密钥只通过环境变量传入。配置文件里保存的是环境变量名称，不是密钥本身。

## 接入任意判断服务

`http_decision` 可以连接任何会接收和返回 JSON 的服务。它可以是规则引擎、传统分类器、本地推理服务或第三方 API。

```toml
[[processors]]
type = "http_decision"
url = "http://127.0.0.1:9000/decide"
token_env = "DECISION_SERVICE_TOKEN"
```

请求体是标准化后的资讯对象。服务只需返回：

```json
{
  "accept": true,
  "annotations": {
    "category": "technology",
    "score": 0.91
  }
}
```

`accept` 决定是否保留，`annotations` 会跟随项目进入后续处理和输出。

## 编写自己的适配器

```python
from collections.abc import Iterable

from information_filter.interfaces import Source
from information_filter.models import InformationItem


class MySource(Source):
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def collect(self) -> Iterable[InformationItem]:
        yield InformationItem(
            id="item-1",
            title="Example",
            content="Collected by a custom source",
            source="my-source",
        )
```

在配置中使用 `模块名:类名`：

```toml
[[sources]]
type = "my_package.sources:MySource"
endpoint = "https://example.com"
```

`Processor` 返回资讯对象表示保留，返回 `None` 表示过滤掉。`Sink.write()` 接收已经通过全部处理器的资讯列表。完整的最小示例见 [`examples/my_plugins.py`](examples/my_plugins.py)。

## 运行方式

```bash
# 检查配置
aif validate -c pipeline.toml

# 执行一次
aif run -c pipeline.toml

# 查看内置插件
aif plugins
```

定时任务可以直接调用 `aif run`：

```cron
15 * * * * cd /path/to/project && . .venv/bin/activate && aif run -c pipeline.toml
```

## 处理流程

1. 每个 Source 产出统一的 `InformationItem`。
2. 状态库按来源和项目标识生成指纹，跳过已经完成的内容。
3. Processor 按配置顺序执行；任意一步返回 `None`，该项目就被过滤。
4. 通过全部处理器的项目会写入每个 Sink。
5. 只有全部输出成功后，项目才会记入去重状态。

去重记录使用处理前的输入指纹。Processor 即使改写 ID、来源或 URL，下一轮仍能识别原始资讯。被过滤掉的资讯不会写入状态库，因此规则变化后仍可重新评估。

多个输出端无法组成跨系统事务。输出适配器应尽量使用项目指纹或业务 ID 实现幂等写入，避免某个输出端失败重试时产生重复数据。

## 开发

```bash
pip install -e '.[dev]'
ruff check .
pytest
```

项目兼容 Python 3.10、3.11 和 3.12；发布前测试使用 Python 3.10 验证。

## 安全

- 不要把 API Key、Token、生产数据库、日志或真实数据提交到仓库。
- 使用 `token_env` 引用环境变量。
- 为避免凭据泄露，内置 HTTP 适配器会拒绝更改域名、协议或端口的重定向。
- HTTP 适配器会把目标地址视为可信配置；部署公开配置编辑器时，应自行限制内网地址和允许的域名。
- 自定义插件是本地 Python 代码，只加载你信任的模块。

## License

[MIT](LICENSE)
