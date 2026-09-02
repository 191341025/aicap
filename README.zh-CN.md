# aicap

[English](https://github.com/191341025/aicap/blob/main/README.md)

把终端里实际发生的一切结构化记录下来，让 AI 助手能直接读取，不用再来回复制粘贴命令输出或截图。

## 要解决的问题

跟 AI 编程助手协作时的一个常见流程：AI 给出一条命令，你把它复制到另一个终端（本地 shell、远程 SSH 会话，随便什么）里执行，再把输出复制回去让 AI 看到发生了什么。这个来回搬运的过程慢，还容易搬错、漏搬——命令一多或者输出一长，尤其明显。

## aicap 做了什么

`aicap` 接管一个交互式 shell 会话。你只需要启动一次、指定一个目录，之后这个终端用起来跟平时一模一样——同一个 shell、同一套别名、同一个 PATH、什么都没变——你敲的每一条命令都会被记录下来（命令文本、输出内容、退出码），以结构化文件的形式存进那个目录。想让 AI 看看刚才发生了什么，直接告诉它去哪个目录看就行。

从你这边看，终端没有任何变化。`aicap` 夹在你和真实 shell 之间，把一切原样镜像回你的屏幕，同时在旁边悄悄写一份结构化的副本到磁盘。

## 支持平台

- Windows：Windows PowerShell 5.1（默认）—— 这是经过大量真实测试的路径。PowerShell 7（`pwsh`）也支持，用 `aicap start <log_dir> --shell pwsh` 即可，但目前实际测试还很少。
- Linux / macOS：bash、zsh

不支持 fish 和 cmd.exe。

## 安装

```bash
pipx install aicap
```

装命令行工具推荐用 [`pipx`](https://pipx.pypa.io/)：它会把 `aicap` 装进一个独立的隔离环境，不会跟你其他 Python 项目的依赖打架，同时 `aicap` 命令本身在哪儿都能直接用。在比较新的 Linux 发行版上，这也是下面几种方式里**唯一能稳定用的**（见下面的提示）。

还没装 `pipx`？

```bash
# Debian/Ubuntu（Debian 12+、Ubuntu 23.04+）
sudo apt install pipx
pipx ensurepath

# macOS
brew install pipx
pipx ensurepath

# 其他平台（含 Windows）
pip install --user pipx
pipx ensurepath
```

上面没覆盖到的情况，参考 [pipx 官方安装指南](https://pipx.pypa.io/latest/how-to/install-pipx.html)。

普通 `pip` 在大多数系统上也能装：

```bash
pip install aicap
```

> **提示：** 在比较新的 Linux 发行版上（Debian 12+、Ubuntu 23.04+，以及其他遵循 [PEP 668](https://peps.python.org/pep-0668/) 规范的系统），像上面这样直接 `pip install`——甚至 `pip install --user`——默认会被拦下，报 `error: externally-managed-environment`，这是系统故意这么设计的，就是为了把你导向 `pipx`（或者虚拟环境）来装这类独立命令行工具。如果遇到这个报错，改用上面的 `pipx` 安装方式就行。

需要 Python 3.9+。

## 快速上手

`aicap start` 只接受一个参数：日志要写到哪个目录。**这个目录跟你在哪工作没有任何关系**——它只是日志的存放位置。大部分人会固定用一个地方（比如自己主目录下的某个目录），不管当下在跑哪个项目都指向同一个日志目录，这样 AI 助手永远知道去哪看，不用每次都变。

1. 打开一个终端，去你实际要工作的地方（比如某个项目目录），然后启动一个会话：

   ```bash
   # bash / zsh
   aicap start ~/aicap-logs
   ```

   ```powershell
   # PowerShell（注意用 $HOME，不要用 ~ —— PowerShell 不会像 bash 那样自动展开 ~）
   aicap start $HOME\aicap-logs
   ```

   这**不会**改变你终端当前所在的工作目录——你还是待在你原来那个地方。这里传的路径只是决定录制内容写到哪，可以是任意路径，跟你手头的项目完全没有关系。

   你会看到：

   ```
   aicap: recording started, writing to /home/you/aicap-logs
   aicap: session id 20260101-120000-a1b2c3
   ```

2. 像平时一样正常使用这个终端，一切看起来都没有区别。

   ```bash
   $ npm test
   $ git status
   $ python train.py --epochs 5
   ```

3. 用完之后，像平时一样退出这个 shell：

   ```bash
   $ exit
   ```

   ```
   aicap: recording finished, session 20260101-120000-a1b2c3 saved
   ```

4. 把 `~/aicap-logs`（或者你传给 `start` 的那个路径）告诉你的 AI 助手。最常用的几个入口：

   - `STATUS.md` —— 一份简短、人类可读的摘要，列出最近几条命令和退出码。**优先看这个文件。**
   - `latest.log` —— 最近一条已完成命令的完整输出。
   - `sessions/<session-id>/index.json` —— 本次会话里每条命令的结构化元数据（命令文本、时间戳、退出码、输出文件位置、是否完整）。
   - `sessions/<session-id>/commands/NNNN-*.log` —— 任意一条具体命令的完整输出。

不启动新录制的情况下，随时查看某次会话（包括已经结束的）：

```bash
aicap status ~/aicap-logs
```

## 大致原理

`aicap` 会在一个伪终端（pseudo-terminal）背后启动你真实的 shell（PowerShell、bash 或 zsh）作为子进程——这跟 `tmux`、`ssh` 这类工具用的是同一套机制。它把你的按键原样转发给这个 shell，再把它的输出原样镜像回你的屏幕——`aicap` 从不解释或拦截你敲的任何内容，包括 Ctrl+C。与此同时，一个很小的 shell 钩子会告诉 `aicap` 每条命令什么时候开始、什么时候结束、退出码是多少——这就是它能把录制内容按命令切分成一个个独立文件的方式。

## 已知限制

- **Windows 上，正在执行的命令无法用 Ctrl+C 打断。** 这是 Windows ConPTY 向被托管的子进程投递控制台控制事件这个机制本身的平台限制，不是 `aicap` 代码能绕开的问题。除了"打断长时间运行的命令"之外，其它一切（命令记录、退出码、正常输入输出）在 Windows 上都正常。
- **嵌套 SSH 不会被拆分成独立的命令。** 如果你在被录制的会话里再 `ssh` 到另一台机器，`aicap` 看不到那个远程 shell 内部发生了什么——整个 SSH 会话会被当作一条"大命令"，从 `ssh` 启动一直记录到它退出。
- 不支持 fish 和 cmd.exe。

## License

MIT —— 见 [`LICENSE`](https://github.com/191341025/aicap/blob/main/LICENSE)。
