# WPS Agent Skill

`wps-agent` 使用 Agent API Key 直连 WPS V7 API，支持读取分享链接、编辑智能文档、下载和回传 Office 文件、更新在线表格单元格以及读写多维表格记录。运行脚本需要 Python 3.10+ 和网络。原件传输和 API 调用使用 Python 标准库。上传智能文档图片和部分 Office 本地解析需要对应的 Python 库。

## 创建 WPS Agent API Key

1. 登录 WPS，打开 **个人中心 → AI 服务 → 我的 AI Agent**。
2. 在「我的 AI 助理」区域选择「个人中心」标签，点击 **创建 AI 助理**。
3. 输入助理名称，例如 `Codex`，按页面提示创建助理并确认授权。
4. 在关闭弹窗前复制并妥善保存密钥。已有助理可在列表中点击「查看授权」，确认它能访问所需的 WPS 能力。

## 配置密钥

skill 按以下顺序读取密钥。

1. 当前进程中非空的 `AGENT_API_KEY`。
2. 在 macOS、Linux 和其他 Unix 系统上，读取用户目录下的 shell 启动文件。优先检查 `$SHELL` 对应的一组，Zsh 依次检查 `.zshrc`、`.zprofile`、`.zshenv`，Bash 依次检查 `.bashrc`、`.bash_profile`、`.bash_login`，最后检查 `.profile`。未知 shell 时先检查 Zsh 一组。
3. 本地 JSON 配置文件。

shell 文件只接受形如 `export AGENT_API_KEY='密钥'` 的独立单行赋值。脚本按文本读取，跳过需要展开变量或执行命令的写法。如果密钥由 shell 动态生成，请通过已导出的环境变量或 JSON 文件提供。

### 本地配置文件

从图形界面启动 Codex 时，可以把密钥保存在 `~/.config/wps-agent/config.json`。如果设置了绝对路径形式的 `XDG_CONFIG_HOME`，文件位置为 `$XDG_CONFIG_HOME/wps-agent/config.json`。

```json
{
  "api_key": "你的 Agent API Key"
}
```

在本地编辑器中创建文件并替换占位符。macOS 和 Linux 上可用 `chmod 600 ~/.config/wps-agent/config.json` 将文件限制为仅当前用户可读写。

### 环境变量

将密钥配置到运行 skill 的进程中，变量名为 `AGENT_API_KEY`。以 macOS 的 zsh 为例，在 `~/.zshrc` 中加入以下一行，将占位符替换为刚才复制的密钥。

```zsh
export AGENT_API_KEY='你的 Agent API Key'
```

执行 `source ~/.zshrc` 可让当前终端立即获得变量。从图形界面启动 Codex 时，skill 也可以直接读取上面的单行赋值。请将真实密钥保留在本地，避免粘贴到聊天或提交到仓库。

WPS 官方文档没有注明 Agent API Key 的固定有效期。实测使用 Key 换取的 Access Token 有效期为 12 小时，令牌到期后可用同一把 Key 重新换取。如果遗失或泄露密钥，在助理列表中点击「重置密钥」。旧密钥会立即失效，重置后用新密钥更新正在使用的配置。 [WPS 账号接入 Agent 身份认证说明](https://www.kdocs.cn/l/cpSf5NCLgM4i)

## 安装和试用

在仓库根目录运行以下命令，将整个 skill 目录复制到 Codex 的用户 skills 目录。

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R wps-agent "${CODEX_HOME:-$HOME/.codex}/skills/"
```

在 Codex 中可用 `$wps-agent` 指定此 skill，例如「用 `$wps-agent` 总结这篇金山文档」。也可在仓库根目录直接验证读权限。

```sh
python3 wps-agent/scripts/document.py inspect --link 'https://www.kdocs.cn/l/cpSf5NCLgM4i'
```

各格式的读写用法见 [SKILL.md](wps-agent/SKILL.md)。
