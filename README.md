# FundCraft

个人基金数据分析与红利低波策略看板，主要功能包括：

* 基金历史净值抓取与本地存储
* 基金收益率、最大回撤、波动率等指标计算
* 红利低波策略的择时评分与买入信号判断
* Supabase 云数据库读写与同步
* Streamlit 本地调试与云端部署展示

## 秘钥加密用法

如果你要把 `url` 和 `key` 以密文方式写入 `.streamlit/secrets.toml`，可以分别执行两次加密脚本，每次只加密一个值：

```powershell
python .\scripts\encrypt_secret_value.py
```

脚本会依次要求你输入：

1. 需要加密的明文内容
2. 加密口令

然后输出一段可直接写入 `secrets.toml` 的密文。你分别对 `url` 和 `key` 运行两次即可。

示例配置格式如下：

```toml
[supabase]
url = "enc:..."
key = "enc:..."
```

解密时，程序会从 `.streamlit/secrets.toml` 读取密文，并根据口令自动解密。

## 登录与解密

`access_password` 用于登录校验，支持在 `.streamlit/secrets.toml` 中配置为列表，多个口令都可以通过验证：

```toml
[security]
access_password = ["123456", "654321"]
```

登录通过后，页面会再要求你输入单独的解密口令。只有输入解密口令后，程序才会开始读取和解密 Supabase 的 `url` 与 `key`。

## Stage 5 本地展示运行方式

在项目根目录下直接启动 Streamlit：

```powershell
streamlit run .\app.py
```

启动后会进入全量展示页，支持查看基金总览、单基金明细、净值走势、回撤走势和同步状态。

## 数据库

https://supabase.com/