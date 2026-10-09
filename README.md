# endfield-scanner

**《明日方舟：终末地》扫码登录抢码脚本**

从屏幕自动获取二维码，并自动扫码完成登录。

主要供学习与技术研究使用，请勿用于商业用途。**账号风险自负，细节见 [LICENSE](LICENSE)。**
本项目主要由 AI 编写。

---

## 安装

需要 **Python 3.11 或更高**。装好 Python 后，**在项目目录里**（下面所有命令也都在
这个目录里执行）创建虚拟环境：

```bash
python -m venv .venv
```

**Windows（用 PowerShell）** —— 激活并安装：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e .
```

> 如果提示脚本被禁止运行，先执行一次：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

**macOS** —— 激活并安装：

```bash
source .venv/bin/activate
pip install -e .
```

### 关于虚拟环境

上面两步会把工具装进 `.venv` 这个**虚拟环境**（独立于系统 Python，不污染全局）。
装好后可执行文件在：

| 平台 | 可执行文件位置 |
|---|---|
| Windows | `.\.venv\Scripts\endfield-scanner.exe` |
| macOS | `.venv/bin/endfield-scanner` |

**命令行提示符前面出现 `(.venv)` 就说明环境是激活的**，这时可以直接敲
`endfield-scanner`。本 README 后面都按这种写法。

如果新开了一个终端窗口，或者提示符前没有 `(.venv)`，先重新激活：

```powershell
.\.venv\Scripts\Activate.ps1      # Windows
```
```bash
source .venv/bin/activate         # macOS
```

> 不想每次激活也可以直接用完整路径，例如
> `.\.venv\Scripts\endfield-scanner.exe doctor`（Windows）、
> `.venv/bin/endfield-scanner doctor`（macOS）。
>
> 用完想退出虚拟环境，敲 `deactivate`。

macOS 还需要授权屏幕录制：**系统设置 → 隐私与安全性 → 屏幕录制** →
勾选你的终端 → **重启终端**。不授权工具无法工作。

### 自检

```bash
endfield-scanner doctor
```

每一项都是 ✔ 才算就绪。它还会用一张自造的二维码测试解码器。

---

## 使用

### 1. 登录一次，拿到 token

```bash
endfield-scanner login
```

用手机「森空岛」App 扫终端里的二维码（App：个人中心右上角 → 扫一扫），
手机上点确认。成功后会打印 `✔ token 可用`。**只需做一次。**

已经有 token 了？用 `endfield-scanner login --paste` 直接粘贴。

### 2. 抢码

```bash
endfield-scanner sniff
```

把游戏登录页面切到屏幕上，看到这些就说明成了：

```
[   3.25s] HIT     发现扫码登录码 scanId=3f7a1c9e...
[   3.49s] HIT     scanId 3f7a1c9e… 已代为扫码（账号 139****0000）
[   6.13s] HIT     scanId 3f7a1c9e… 已代为确认登录
[   6.22s] SUCCESS 登录成功，客户端应该已经登上你的账号了。
```

默认全屏扫描，不需要指定区域。

| 参数 | 作用 |
|---|---|
| `-v` | 打印所有请求、逐帧耗时、状态变化。排障必用 |
| `--save-capture` | 识别到码时把那一帧存到 `./captures/` |
| `--timeout 60` | 盯 60 秒后退出（默认不限，Ctrl+C 退出） |
| `--keep-going` | 抢到一个后继续盯下一个 |
| `--region 左,上,宽,高` | 只扫屏幕的一块区域 |

---

## 命令

| 命令 | 作用 |
|---|---|
| `endfield-scanner doctor` | 环境自检（**遇到问题先跑这个**） |
| `endfield-scanner login` | 扫码登录，保存 token |
| `endfield-scanner login --paste` | 手动粘贴 token |
| `endfield-scanner login --logout` | 删除已保存的 token |
| `endfield-scanner status` | 查看 token 是否还有效 |
| `endfield-scanner token --show` | 显示完整 token（谨慎） |
| `endfield-scanner sniff` | **主力命令**：盯屏幕抢码 |
| `endfield-scanner selftest` | 用自造二维码验证解码链路 |

---

## 常见问题

**敲 `endfield-scanner` 提示找不到命令**

说明虚拟环境没激活（提示符前面没有 `(.venv)`）。先 `cd` 到项目目录，再激活一次：

```powershell
.\.venv\Scripts\Activate.ps1      # Windows
```
```bash
source .venv/bin/activate         # macOS
```

**识别不到二维码**

带上日志和截图跑一次，然后看 `./captures/` 里的图 —— 那正是工具看到的画面：

```bash
endfield-scanner sniff -v --save-capture
```

**报「截屏失败」**

macOS 去授权屏幕录制（见上文）并重启终端。

**报「没有找到 token」/「token 已失效」**

跑 `endfield-scanner login` 重新登录。

**游戏显示「二维码已过期」**

多半是码本身超时了，让游戏刷新出新码再试。用 `-v` 看状态变化能确认。

**Windows 中文显示成乱码**

换成 **Windows Terminal** 就好。

---

## token 存在哪里

token 等同于你的账号，所以优先存在系统安全存储里：

| 平台 | 位置 |
|---|---|
| macOS | 钥匙串，搜 `endfield-scanner` |
| Windows | 凭据管理器 → Windows 凭据 |

安全存储不可用时会退回明文文件，并且**会明确警告你**：

| 平台 | 位置 |
|---|---|
| Windows | `%APPDATA%\endfield-scanner\token.json` |
| macOS | `~/Library/Application Support/endfield-scanner/token.json` |
| Linux | `~/.config/endfield-scanner/token.json` |

也可以用环境变量，不落盘：

```bash
export ENDFIELD_ACCOUNT_TOKEN="你的 token"
# Windows PowerShell: $env:ENDFIELD_ACCOUNT_TOKEN="你的 token"
```

删除：`endfield-scanner login --logout`

---

## 项目结构与依赖

```
endfield-scanner/
├── src/endfield_scanner/
│   ├── cli.py                命令行入口
│   └── core/
│       ├── api.py            与鹰角服务器通信
│       ├── auth.py           token 的获取与安全保存
│       ├── qrlogin.py        扫码登录
│       ├── qrdecode.py       二维码识别
│       ├── frames.py         帧源（截屏 / 图片）
│       ├── sniper.py         抢码主逻辑
│       ├── httplog.py        请求日志（-v 用）
│       └── paths.py          跨平台配置目录
├── tests/                    测试（108 个用例）
└── pyproject.toml
```

| 依赖 | 用途 |
|---|---|
| zxing-cpp | 二维码识别（比 OpenCV 自带检测器更快、对小码更稳） |
| opencv-python | 图像处理、生成测试二维码 |
| mss | 屏幕截图 |
| httpx | 网络请求 |
| keyring | token 安全存储 |

---

## 致谢

* [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp)
* [nonebot-plugin-skland](https://github.com/FrostN0v0/nonebot-plugin-skland)
* [ProbiusOfficial/Skland_API](https://github.com/ProbiusOfficial/Skland_API)

---

## TODO

直播流取流（B 站 / 抖音）**尚未实现**。`--input stream:<平台>:<房间号>` 的语法
会被识别但提示未启用。相关代码入口与注意事项见 `sources/__init__.py`。
