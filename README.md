# ha-oai-fork

Home Assistant 官方 `openai_conversation` 集成的 fork，加了两件事：

1. **可自定义 `base_url`** —— 用任何 OpenAI 兼容网关驱动 HA 语音助手
2. **火山方舟（Volcengine Ark）兼容层** —— 官方组件直连 Ark 会 400 / 500，这里修掉了

上游基线：**Home Assistant 2026.6.0**（`openai` lib 2.21.0）

> 仓库第一个 commit 是**未改动的官方源码**，第二个 commit 才是 patch。
> 所以 `git diff HEAD~1` 就是完整、精确的改动集 —— HA 升级后重新对齐时很有用。

---

## 为什么需要 Ark 兼容层

只加 `base_url` 是**不够**的。以下是逐条 curl 实测结果：

| 请求字段 | Ark `/api/v3/responses` |
|---|---|
| `prompt_cache_retention` | ❌ 400 `unknown field`（官方组件默认发 `"24h"`，必炸） |
| `user` | ❌ 400 `unknown field` |
| `tools[].search_context_size` | ❌ 400 `unknown field` |
| `tools[].user_location` | ❌ 400 `unknown field` |
| `service_tier` / `store` / `top_p` / `temperature` / `max_output_tokens` / `parallel_tool_calls` | ✅ |
| `{"type": "web_search"}`（裸） | ✅ 真联网出结果 |
| `{"type": "web_search_preview"}` | ❌ unknown type |
| `GET /models` | ✅（所以 config flow 的连通性校验无需改动） |

### 最隐蔽的坑：流式 function call 的 `arguments` 是 `null`

官方 OpenAI 在 `response.output_item.added` 事件里给出的 function call `arguments` 初值是空字符串 `""`；
**Ark 给的是 `null`**。于是官方组件里这行：

```python
current_tool_call.arguments += event.delta
```

直接抛 `TypeError: unsupported operand type(s) for +=: 'NoneType' and 'str'`。

症状极具误导性：**纯文本对话完全正常，一旦让它控制设备就 HTTP 500**。

---

## 改动清单

| 文件 | 改动 |
|---|---|
| `const.py` | 新增 `CONF_BASE_URL`、`DEFAULT_BASE_URL`、`OPENAI_ONLY_REQUEST_PARAMS`、`OPENAI_ONLY_WEB_SEARCH_FIELDS` |
| `config_flow.py` | 配置表单加 `base_url` 输入项；`validate_input()` 与 `_get_location_data()` 的 `AsyncOpenAI(...)` 传入 `base_url` |
| `__init__.py` | `async_setup_entry()` 的 `AsyncOpenAI(...)` 传入 `base_url` |
| `entity.py` | ① 非官方 endpoint 时剥离 Ark 不认的字段；② `arguments` 为 `None` 时归一化成 `""`，并给 `json.loads(... or "{}")` 兜底 |
| `manifest.json` | 加 `version` 字段（custom integration 必需） |
| `strings.json` / `translations/en.json` | `base_url` 的 UI 文案 |

兼容层是**条件生效**的 —— 只有当 `base_url` 不等于官方 `https://api.openai.com/v1` 时才剥离字段，
所以接官方 OpenAI 时行为与上游完全一致。

---

## 安装

```bash
# 复制到 HA 配置目录
cp -r openai_conversation /config/custom_components/openai_conversation

# 从 macOS 传过去的话，记得清掉 AppleDouble 垃圾文件
find /config/custom_components/openai_conversation -name '._*' -delete

# 重启 HA
docker restart homeassistant
```

`custom_components/` 下的同名 domain 会**覆盖** built-in 集成，HA 只会打一条 warning：

```
WARNING [homeassistant.loader] We found a custom integration openai_conversation
which has not been tested by Home Assistant...
```

这是预期行为。好处是不用改 domain，官方 UI 文案和后续 re-sync 都省事。

---

## 配置（以火山方舟豆包为例）

添加集成时填：

- **API Key**：方舟 API Key
- **Base URL**：`https://ark.cn-beijing.volces.com/api/v3`

然后在 conversation subentry 里：

- 关掉 *Recommended settings*（否则看不到高级选项）
- **Model**：`doubao-seed-2-0-mini-260428`（或其他方舟 model / endpoint id）
- **Web search**：打开即可获得真实联网能力
- **Control Home Assistant**：选 `Assist`，LLM 才拿到设备控制 tools

---

## HA 升级后如何重新对齐

```bash
# 1. 取新版官方源码
docker cp homeassistant:/usr/src/homeassistant/homeassistant/components/openai_conversation ./upstream-new

# 2. 看本 fork 的 patch 到底改了什么
git diff <上游基线commit> <patch commit>

# 3. 把 patch 重新应用到新版源码上，更新 manifest 里的 version
```

---

## License

上游代码遵循 Home Assistant 的 Apache License 2.0。本 fork 沿用同一 license。
