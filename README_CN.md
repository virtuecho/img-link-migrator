# IMG Link Migrator

[English](README.md) | **简体中文**

这是一个完全独立的桌面与命令行工具，用于把 Markdown 文件中的外链图片迁移到
[ImgBB](https://imgbb.com/)，并将原图片链接替换为 ImgBB 链接。

项目主要面向 Obsidian vault，也可以处理任意 UTF-8 编码的 Markdown 文件或文件夹。
项目使用通用名称 **IMG Link Migrator**，ImgBB 是目前第一个也是唯一支持的图片
托管平台。

## 功能

- 通过原生 GUI 选择单个 Markdown 文件或整个文件夹。
- 正式迁移前先安全扫描，不修改文件。
- 可以迁移所有外链图片，也可以限定 `xhscdn.com` 等指定来源域名。
- 识别 Markdown 行内图片、HTML `<img>` 标签和 Markdown 引用式图片。
- 自动跳过 YAML 属性区、代码块、行内代码、隐藏文件夹和现有 ImgBB 链接。
- 先下载并验证源图片，再执行上传，同时检查 ImgBB 的 32 MB 大小限制。
- 使用 ImgBB API v1 和 `multipart/form-data` 上传图片。
- 显示每张图片的处理进度、成功、缓存复用和失败原因。
- 下载或上传失败时使用指数退避自动重试。
- 可以在 GUI 中手动重试失败项目。
- 相同 URL 只处理一次；内容完全相同的图片也会去重。
- 每张图片上传完成后立即原子替换对应 Markdown 链接，再处理下一张图片。
- 只替换上传成功的链接，失败链接保持不变。
- 如果文件在扫描后被其他程序修改，会停止写入，避免覆盖新内容。
- 使用原子写入，降低写入中断造成文件损坏的风险。
- 可以选择是否在替换前备份发生改动的 Markdown 文件。
- 支持取消正在进行的迁移。
- API key 不会写入文件、报告、备份或持久化缓存。

## 环境要求

- Python 3.9 或更高版本
- 使用 GUI 时需要 Tk
- ImgBB API key

Homebrew 将 Tk 与 Python 分开提供。如果 GUI 提示找不到可用的 Tk，请查找并安装
与所选 Python 解释器匹配的版本：

```bash
brew search python-tk
```

Tk 仅用于 GUI。

## 快速开始

### GUI

在 macOS 上双击 `launch-gui.command`，或者运行：

```bash
python3 img_link_migrator.py --gui
```

操作步骤：

1. 选择一个 Markdown 文件或 vault 文件夹。
2. 粘贴 ImgBB API key。它只会保存在本次运行的内存中。
3. 根据需要填写一个或多个来源域名，多个域名使用英文逗号分隔。
4. 选择是否在修改文件前创建备份。
5. 点击 `Scan`，预览检测到的图片链接。
6. 点击 `Start migration`，上传图片并替换成功项目的链接。
7. 如果存在失败项目，点击 `Retry failed`。

来源域名留空时会处理所有外链图片。ImgBB 自身域名始终会被排除，避免重复上传。

### CLI

CLI 指在终端中输入命令。下面的命令需要在项目目录中执行，也就是当前目录里应该能
看到 `img_link_migrator.py`。如果终端当前不在项目目录，可以输入 `cd`、空格，
然后把项目文件夹从 Finder 拖入终端，按回车。

#### API key 填在哪里

GUI 和 CLI 的填写位置不同：

- **GUI**：把 API key 粘贴到界面中的“ImgBB API key”输入框。
- **CLI（推荐）**：在当前终端中建立名为 `IMGBB_API_KEY` 的临时变量，并把 API key
  作为这个变量的值。
- **CLI（不推荐）**：使用 `--api-key "你的密钥"` 直接放在命令中。这种写法可能
  被终端历史或进程列表记录。

`IMGBB_API_KEY` 是固定的变量名，不需要修改。设置环境变量的命令由当前 shell 决定，
fish、zsh 和 bash 的 `read` 语法不能混用。可以运行 `echo $SHELL` 查看正在使用的
shell，然后只执行下面对应的一组命令。

##### fish

如果终端提示符类似 `目录 (main)>`，并且错误信息中出现
`See help identifiers`，通常正在使用 fish。

1. 复制自己的 ImgBB API key。
2. 运行下面的输入命令。
3. 终端显示 `API key:` 后粘贴密钥并按回车。fish 可能用 `*` 遮罩输入。

```fish
read --silent --global --export --prompt-str 'API key: ' IMGBB_API_KEY
```

确认已经设置，不显示密钥内容：

```fish
set --query IMGBB_API_KEY; and echo 'API key 已设置'; or echo 'API key 未设置'
```

使用结束后清除：

```fish
set --erase --global IMGBB_API_KEY
```

##### zsh

```zsh
read -s "IMGBB_API_KEY?API key: "; echo
export IMGBB_API_KEY
```

确认已经设置：

```zsh
if [[ -n "$IMGBB_API_KEY" ]]; then
  echo "API key 已设置"
else
  echo "API key 未设置"
fi
```

使用结束后清除：

```zsh
unset IMGBB_API_KEY
```

##### bash

```bash
IFS= read -r -s -p "API key: " IMGBB_API_KEY; echo
export IMGBB_API_KEY
```

确认已经设置：

```bash
if [[ -n "$IMGBB_API_KEY" ]]; then
  echo "API key 已设置"
else
  echo "API key 未设置"
fi
```

使用结束后清除：

```bash
unset IMGBB_API_KEY
```

设置后，这个终端会话中执行的迁移命令会自动读取 API key，不需要在每条命令中再次
填写。变量只对当前终端会话有效，关闭该会话后会失效。

API key 只在执行 `--apply` 实际上传时需要。仅扫描和打开 GUI 时不需要提前设置。

如果仍要把 API key 直接写在迁移命令里，完整形式如下。把
`YOUR_IMGBB_API_KEY` 换成真实密钥：

```bash
python3 img_link_migrator.py \
  --apply \
  --api-key "YOUR_IMGBB_API_KEY" \
  "$HOME/Documents/MyVault"
```

#### 目标路径是什么

“目标”是要扫描的 Markdown 文件或文件夹路径：

- `./note.md`：当前目录中的 `note.md` 文件。
- `./notes`：当前目录中的 `notes` 文件夹。它只是示例；如果没有这个文件夹，就要
  换成自己的路径。
- `"$HOME/Documents/MyVault"`：位于当前用户文档目录中的示例 vault。

`.` 表示终端当前目录，`$HOME` 表示当前用户的主目录。路径含有空格或中文时，应使用
英文双引号包住整个路径。

在 macOS 中，最简单的填法是先输入命令和一个空格，再把目标文件或文件夹从 Finder
拖进终端；终端会自动填入实际路径。

#### 最常用的完整流程

以下示例中的 `"$HOME/Documents/MyVault"` 只是演示路径，执行前必须换成自己的文件
或文件夹路径。

第一步，只扫描并查看会处理哪些链接，不上传、不改文件：

```bash
python3 img_link_migrator.py "$HOME/Documents/MyVault"
```

第二步，确认扫描结果后执行迁移。程序会读取前面设置的 `IMGBB_API_KEY`：

```bash
python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"
```

如果只想处理某个来源域名，添加 `--include-host`。下面用小红书 CDN 举例，这个参数
同样可以填写其他图片域名：

```bash
python3 img_link_migrator.py \
  --apply \
  --include-host xhscdn.com \
  "$HOME/Documents/MyVault"
```

关闭默认备份：

```bash
python3 img_link_migrator.py \
  --apply \
  --no-backup \
  "$HOME/Documents/MyVault"
```

迁移后生成 JSON 报告：

```bash
python3 img_link_migrator.py \
  --apply \
  --report report.json \
  "$HOME/Documents/MyVault"
```

### CLI 模式与参数

CLI 有三种基本写法：

```text
打开 GUI：python3 img_link_migrator.py --gui
只扫描示例：python3 img_link_migrator.py "$HOME/Documents/MyVault"
迁移示例：  python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"
```

文档中常见的命令格式符号只用于说明，不要原样输入：

| 格式符号 | 含义 | 示例 |
|---|---|---|
| `[内容]` | 可选内容，可以完全不写；方括号本身不输入。 | `[选项]` 表示可以添加选项，也可以不添加。 |
| `<内容>` | 必须换成自己的实际值；尖括号本身不输入。 | `<目标>` 应换成 Markdown 文件或文件夹路径。 |
| `...` | 前一项可以重复多次；三个点本身不输入。 | `<目标> ...` 表示可以连续填写多个路径。 |
| 大写单词 | 需要替换的值，不是固定文字。 | `KEY` 换成 API key，`SECONDS` 换成秒数。 |

| 运行方式 | 是否下载或上传 | 是否修改 Markdown | 什么时候使用 |
|---|---:|---:|---|
| 指定目标，但不写 `--apply` | 否 | 否 | 首次检查时使用，只列出检测到的外链图片。 |
| 添加 `--apply` | 是 | 是，只替换上传成功项 | 确认扫描结果后执行正式迁移。 |
| 添加 `--gui` | 点击 `Start migration` 后才会请求 | 用户确认后才会修改 | 使用桌面界面选择目标和设置参数。 |

`--gui` 和 `--apply` 不能同时使用。

“默认值”表示不写该参数时程序自动采用的设置。“开关”表示参数后面不填写值：写出它
就是开启相应行为，不写就是关闭。

| 参数 | 你需要填写什么 | 不写这个参数时 | 作用与示例 |
|---|---|---|---|
| `目标路径` | 一个或多个 Markdown 文件或文件夹路径，不要输入单词 `targets`。 | 扫描和迁移模式必须有目标；单独运行程序会打开 GUI。 | `"$HOME/Documents/MyVault"`；多个目标写成 `"folder-a" "note-b.md"`。 |
| `--gui` | 后面不填值。 | 使用 CLI；有目标时默认只扫描。 | `python3 img_link_migrator.py --gui` |
| `--apply` | 后面不填值。 | 只扫描，不下载、不上传、不修改文件。 | `python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"` |
| `--api-key KEY` | 把 `KEY` 换成真实 ImgBB API key。 | 自动读取环境变量 `IMGBB_API_KEY`；两者都没有时无法执行迁移。 | `--api-key "YOUR_IMGBB_API_KEY"`；推荐使用前文的环境变量方式。 |
| `--expiration SECONDS` | 把 `SECONDS` 换成自动删除前的秒数。 | 默认 `0`，请求永久保存。 | 临时保存只能填写 `60`–`15552000`，例如 `--expiration 600`。 |
| `--retries N` | 把 `N` 换成失败后的自动重试次数。 | 默认 `3`，即首次失败后最多再重试 3 次。 | 范围 `0`–`10`；`--retries 0` 表示只尝试一次。 |
| `--include-host DOMAIN` | 把 `DOMAIN` 换成只想处理的来源域名。 | 不限制来源域名，处理所有检测到的外链图片。 | `--include-host xhscdn.com`；可重复使用或用英文逗号分隔。 |
| `--exclude-host DOMAIN` | 把 `DOMAIN` 换成不想处理的来源域名。 | 不增加额外排除项；ImgBB 自身域名仍始终排除。 | `--exclude-host example.com`；可重复使用或用英文逗号分隔。 |
| `--state-dir PATH` | 把 `PATH` 换成缓存和备份目录。 | 使用对应系统的应用数据目录。 | `--state-dir "./migrator-state"` |
| `--no-backup` | 后面不填值。 | 默认在修改 Markdown 前创建备份。 | 添加后关闭本次备份：`--apply --no-backup "$HOME/Documents/MyVault"`。 |
| `--report PATH` | 把 `PATH` 换成 JSON 报告文件位置。 | 不生成 JSON 报告。 | `--report "./report.json"` |
| `-h`、`--help` | 后面不填值。 | 正常执行命令。 | 显示帮助：`python3 img_link_migrator.py --help`。 |

查看程序生成的命令帮助：

```bash
python3 img_link_migrator.py --help
```

## 备份与状态文件

备份默认开启，也可以关闭：

- GUI：取消勾选 `Back up before changes`。
- CLI：添加 `--no-backup`。

URL/图片内容缓存以及可选备份都保存在所选 vault 之外：

- macOS：`~/Library/Application Support/IMG Link Migrator/`
- Windows：`%APPDATA%/IMG Link Migrator/`
- Linux：`$XDG_STATE_HOME/img-link-migrator/`，如果没有设置该变量则使用
  `~/.local/state/img-link-migrator/`

每次备份都会建立一个带时间戳的目录。目录中的 `manifest.json` 会记录原文件绝对
路径与备份副本的对应关系。恢复时，把相应备份副本复制回原路径即可。

持久化缓存会保存源 URL、图片 SHA-256、ImgBB 目标 URL 和过期时间，不会保存
ImgBB API key。

## 支持的 Markdown 格式

```markdown
![说明文字](https://example.com/image.png)

<img src="https://example.com/image.jpg" alt="示例">

![说明文字][image-id]
[image-id]: https://example.com/image.webp
```

普通链接（例如 `[网站](https://example.com/)`）不会被当作图片处理。

## 安全处理流程

完整迁移过程如下：

1. 扫描 Markdown，记录可以替换的图片 URL。
2. 下载并验证第一张源图片。
3. 如果本地存在仍然有效的 ImgBB 缓存链接，则直接复用；否则上传到 ImgBB。
4. 图片上传完成后，立即处理所有引用该 URL 的 Markdown 文件：
   - 如果启用了备份，并且该文件在本次任务中尚未备份，先备份一次原文件。
   - 确认文件从扫描或上一次程序写入后没有被其他程序修改。
   - 使用原子写入立即替换该图片 URL。
5. 确认写入结束后，才开始下载和上传下一张图片。
6. 重复以上步骤，直到任务完成或取消。

同一文件在一次任务中最多备份一次，备份内容是该文件开始迁移前的状态。正常取消时，
已经上传成功的图片链接已经写入，尚未处理的图片保留原链接。

上传响应返回与本地原子写入之间仍存在极短的程序执行间隔。如果进程在该间隔内被系统
强制终止，ImgBB 上可能会暂时留下一张尚未写入 Markdown 的图片；再次运行时会复用
本地缓存并完成替换。

## ImgBB 自动删除时间

默认值为 `0`，表示请求永久保存。如果希望 ImgBB 自动删除图片，可以设置
60 到 15,552,000 秒：

```bash
python3 img_link_migrator.py \
  --apply \
  --expiration 600 \
  "$HOME/Documents/MyVault"
```

已经过期的缓存记录不会被复用。临时图片缓存也不会用于需要永久保存的迁移任务。

## 开发与测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

检查 Python 语法：

```bash
python3 -m py_compile img_link_migrator.py
```

测试使用模拟的 ImgBB 客户端，不会发起网络请求。

## 当前限制

- 只修改 UTF-8 编码的 Markdown 文件。
- 需要浏览器登录状态才能访问的源图片可能无法下载。对于小红书 CDN，程序会自动把
  小红书网站设置为 HTTP Referer，这可以处理许多公开图片链接。
- URL 解析主要支持 Obsidian 和 Markdown 的常见图片语法。自定义插件生成的非标准
  语法可能无法识别。
- 程序不会删除 ImgBB 上的图片。
- 当前支持的图片托管平台为 ImgBB。

## 许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)。
