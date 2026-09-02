# DSH 模型代理流中断问题：根因与修复

## 结论

这次高频 `STREAM_CLOSED` 不是 MCP、OAuth、最大 turn 或 300 秒 idle
timeout 导致的。历史 Python Model Proxy 曾用 `Connection: close` 掩盖不完整
SSE；后续自制 `http.client` 连接池又引入了与 DSH 原生连接栈不同的 TLS、连接
复用和流读取行为。

当前 DSH runner 已改为独立的 Node Model Proxy。它直接使用 Node 22 内置的
Undici `fetch` 连接池访问 DeepSeek，并把 SSE 流转发给 DSH；Python 只负责
启动进程和读取原子预算状态。上游在响应中途失败时，Node 代理销毁未完成的
下游响应，DSH 因此收到 `UND_ERR_SOCKET`/`TRANSPORT` 并沿用自身重试策略。
代理不会自行重放模型 POST。

## 现象与证据

分析目录：
`astra/results/toolathlon-dsh-108-transport-rst-v2`。该批次的前 20 个任务均通过
artifact gate，但产品执行和模型请求表现如下：

| 指标 | 结果 |
| --- | ---: |
| 任务 | 20 |
| DSH 正常完成 / 崩溃 | 3 / 17 |
| evaluator `pass` / `no_pass` | 4 / 16 |
| 模型请求成功 / 失败 | 132 / 59，共 191 次 |
| 模型请求失败率 | 30.9% |
| `SSLEOFError` | 29 |
| `downstream_disconnected` | 13 |
| `IncompleteRead` | 17 |
| DSH `SERVER` / `TRANSPORT` 重试 | 29 / 13 |

17 个 `IncompleteRead` 与 17 个最终 `STREAM_CLOSED` 一一对应。前两类失败分别
变成可重试的 `SERVER` 和 `TRANSPORT`；`IncompleteRead` 发生在代理已经转发
HTTP 200 和部分 SSE 数据之后，不能再改发 502，最终却被 DSH 识别成了
`STREAM_CLOSED`，没有触发重试。

任务没有达到模型请求预算，也没有触发任务 timeout。失败耗时分布也不符合
300 秒 idle timeout：`IncompleteRead` 平均 7.706 秒，范围 2.096–31.464 秒；
`SSLEOFError` 平均只有 0.126 秒。

## 原始 DSH 的错误分类

DSH 直接调用模型时，调用链为：

1. `packages/llm/llm-deepseek/src/adapter.ts` 使用 Node `fetch` 读取响应流。
2. fetch 或响应体读取抛出的网络异常被包装为 `TRANSPORT`。
3. `packages/llm/llm/src/retry-policy.ts` 默认重试 `TRANSPORT`，当前 profile
   最多重试 5 次。
4. 只有响应体以正常 EOF 结束但始终没有 `[DONE]` 时，
   `packages/llm/llm-deepseek/src/sse.ts` 才抛出 `STREAM_CLOSED`。
5. `STREAM_CLOSED` 不在默认重试列表中。

因此，原始 DSH 对真正的 TCP/TLS/socket 中断会走 `TRANSPORT`；本地代理不应
把同一个失败转换成正常 EOF。

## 代理与直连的关键差异

### 1. `Connection: close` 掩盖了传输中断

在本地构造“返回部分 chunked SSE 后断开”的上游，并对响应头及断开方式做
2×2 对照，Node 22.19.0/Undici 6.21.2 的结果为：

| 响应连接语义 | FIN | TCP RST |
| --- | --- | --- |
| HTTP/1.1 持久连接 | `TypeError: terminated` (`UND_ERR_SOCKET`) | `TypeError: terminated` (`UND_ERR_SOCKET`) |
| `Connection: close` | 正常 EOF | 正常 EOF |

因此，仅在代理端设置 `SO_LINGER` 发送 RST 仍不够：如果响应已经声明
`Connection: close`，Undici 仍会把断开当作响应边界。修复前，用 DSH 自己的
`parseSse` 进行 A/B 测试得到：

- 直连故障上游：Undici 抛 `terminated`，DSH adapter 会转换为
  `TRANSPORT`。
- 经过代理：读取正常结束，`parseSse` 抛 `STREAM_CLOSED`。

### 2. 代理曾取消上游连接复用

原始 DSH 的 Undici 会维护连接池。最初的 Python 代理每次请求都新建一个
`http.client.HTTPConnection`/`HTTPSConnection`，并在请求结束后关闭。在 5 次
顺序请求的本地对照中，直连使用 2 个客户端源端口，经过代理则使用 5 个。

这会增加 TCP/TLS 握手次数，可能放大现有上游网络波动，与 29 次快速
`SSLEOFError` 相符。它不是 `STREAM_CLOSED` 错误分类的直接原因。当前实现不再
维护自制 Python 上游连接池，而由 DSH 同版本 Node 的 Undici 管理连接复用和
坏连接淘汰。

## 代码修复

DSH 当前修复位于 `node_model_proxy.mjs`，Python 启动封装位于
`node_model_proxy.py`。完整的流失败处理为：

- 使用 Node 22 内置 Undici `fetch` 连接池，不再由 Python 建立 DeepSeek TLS。
- SSE 数据按读取顺序直接写入下游，不缓冲为完整模型回复。
- 记录 Node、Undici、连接尝试、响应头、字节数和嵌套 transport cause。
- 上游没有返回 HTTP 状态便发生传输失败时，直接销毁未完成的下游响应，不合成
  502；DSH 因而保留原生 `TRANSPORT` 分类。
- 上游在响应头发送后失败时，不追加第二个 HTTP 响应或 chunked 终止块，同样
  销毁未完成的下游响应。
- 请求预算、参数冻结和 `model-usage.jsonl` 结构保持不变。

结果如下：

| 场景 | 下游表现 | DSH 行为 |
| --- | --- | --- |
| 上游完整返回 | 完整 HTTP 响应 | 正常处理 |
| 上游在响应头前失败 | socket 异常断开 | `TRANSPORT`，按原策略重试 |
| 上游在 SSE 中途失败 | 不完整响应体 + 异常断开 | `TRANSPORT`，按原策略重试 |
| 上游正常 EOF 但协议缺少 `[DONE]` | 正常 EOF | `STREAM_CLOSED`，保留原始 DSH 语义 |

没有把 `STREAM_CLOSED` 加入重试列表。这样不会用宽泛重试掩盖真正的 SSE
协议错误，也不需要修改 DSH 源码或其默认重试策略。

## 回归验证

`astra/runners/toolathlon_dsh/tests/test_node_model_proxy.py` 构造一个发送部分 SSE
后断开的上游，检查以下约束：

- 代理已转发 HTTP 200 和部分 SSE 数据；
- 转发响应不包含 `Connection: close`；
- 不包含第二个 HTTP 502 或 `provider_transport_error`；
- 下游连接被异常终止；
- 审计事件记录上游 `UND_ERR_SOCKET` 及 socket 诊断；
- 响应头前断连不会被伪装成 HTTP 502，审计中的 HTTP 状态保持为空；
- 5 次顺序请求复用 Undici 上游连接；
- 中途断流后，下一次请求能通过健康连接成功返回。

修复后再次调用 DSH 自己的 `parseSse` 做同一组故障 A/B：直连返回
`TypeError: terminated`（`ECONNRESET`），经过代理返回
`TypeError: terminated`（`UND_ERR_SOCKET`）。两者都会被 DSH adapter 包装为
`TRANSPORT`，代理路径不再产生 `STREAM_CLOSED`。

运行代理测试：

```bash
cd /home/vagrant/moi-benchmark
python3 -m unittest astra.runners.toolathlon_verified.tests.test_model_proxy
python3 -m unittest astra.runners.toolathlon_dsh.tests.test_node_model_proxy
python3 -m unittest discover -s astra/runners/toolathlon_verified/tests
```

真实验证应使用新的输出目录重跑任务，避免批处理脚本把已有有效 artifact
识别为已完成。修复后的验收重点不是要求上游网络永不失败，而是确认中途断流
在 DSH trajectory 中表现为 `TRANSPORT` 和 `llm/retry`，不再直接结束为
`STREAM_CLOSED`。
