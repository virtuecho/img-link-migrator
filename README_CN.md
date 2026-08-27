# IMG Link Migrator

[English](README.md) | **简体中文**

本仓库包含两个互相独立的图片链接迁移程序：

1. `img_link_migrator.py`：Python Markdown GUI/CLI。
2. `img-link-migrator.command`：处理文本和 Markdown 文件中裸图片 URL 的 macOS
   单文件程序。

两个程序分别拥有自己的运行环境、扫描规则、输入提示、缓存和安全行为。一套程序的
文档不适用于另一套程序。两个程序都支持以下上传目标：

- [ImgBB API v1](https://api.imgbb.com/1/upload)
- 基于 [Chevereto API v1](https://v4-docs.chevereto.com/api/1/file-upload.html)
  的 [PicGo.net API v1.1](https://www.picgo.net/api-v1/?lang=en)

## Python Markdown GUI/CLI

Python 程序主要面向 Obsidian vault 和其他 UTF-8 Markdown 文件。它理解常见的
Markdown 图片语法，不会把文件中的每一个 URL 都当作图片。

### Python 上传平台

| `--provider` 取值 | 上传目标 | API key 环境变量 | API 基础 URL |
| --- | --- | --- | --- |
| `chevereto`（默认） | PicGo.net 或其他 Chevereto 站点 | `CHEVERETO_API_KEY` | 默认 `https://www.picgo.net`；CLI 可使用 `--chevereto-url` 修改。 |
| `imgbb` | ImgBB | `IMGBB_API_KEY` | 固定使用 ImgBB API v1。 |

不同平台使用互相隔离的缓存。切换上传目标时，不会复用其他平台保存的 URL 或图片
内容哈希缓存。

两个平台使用相同的单次任务上传请求限制：每分钟不超过 50 次，上传请求开始时间至少
间隔 1.21 秒。自动重试也必须经过同一个限速器，因此同样计入限制。如果平台返回
`Flooding`、`rate limit` 或 `too many requests`，全部上传会统一暂停 60 秒，然后
自动继续重试。下载、缓存复用和本地文件写入不占用上传名额。

当 PicGo.net 返回 `Duplicated upload` 并提供有效的现有图片 URL 时，两个程序都会把
该 URL 作为成功结果直接复用。随后保存内容哈希；以后即使来源 URL 不同，只要图片
字节完全相同，也会跳过上传请求。

GUI 提供固定的 PicGo.net 选项。Python CLI 选择 `chevereto` 时，会使用
`X-API-Key` 请求头，并把图片放入名为 `source` 的 multipart 字段，发送到
`/api/1/upload`。通过 `--chevereto-url` 指定其他 Chevereto 站点时，程序会使用该
站点上的同一标准接口。

### Python 程序功能

- 通过原生 GUI 选择单个 Markdown 文件或整个文件夹。
- 正式迁移前先安全扫描，不修改文件。
- 可以迁移所有外链图片，也可以限定指定来源域名。
- 识别 Markdown 行内图片、HTML `<img>` 标签和 Markdown 引用式图片。
- 自动跳过 YAML 属性区、代码块、行内代码、隐藏文件夹和所选目标平台的现有链接。
- 先下载并验证源图片，同时执行本地 32 MB 安全限制。
- 显示每张图片的进度、缓存复用、成功和失败详情。
- 两个平台都限制为每分钟最多发送 50 次上传请求。
- 支持自动重试，也可以在 GUI 中手动重试失败项目。
- 相同 URL 只处理一次；内容完全相同的图片也会去重。
- 每张图片完成后立即原子替换对应链接，再处理下一张图片。
- 可以选择是否备份发生改动的 Markdown 文件。
- 如果文件在扫描后被其他程序修改，会停止写入，避免覆盖新内容。
- API key 不会写入文件、报告、备份或持久化缓存。

### Python 程序要求

- Python 3.9 或更高版本
- 正式迁移时需要所选上传平台的 API key
- GUI 需要 Tk

Homebrew 将 Tk 与 Python 分开提供。如果 GUI 提示找不到可用的 Tk，请查找并安装
与所选 Python 解释器匹配的版本：

```bash
brew search python-tk
```

Tk 仅供 GUI 使用。

### Python GUI

在 macOS 上双击 `launch-gui.command`，或者运行：

```bash
python3 img_link_migrator.py --gui
```

操作步骤：

1. 选择一个 Markdown 文件或 vault 文件夹。
2. 保留默认的 **PicGo.net (Chevereto)**，或者改选 **ImgBB**。
3. 粘贴该平台的 API key。它只会保存在本次运行的内存中。
4. 根据需要填写一个或多个来源域名，多个域名使用英文逗号分隔。
5. 选择是否在修改文件前创建备份。
6. 点击 `Scan`，预览检测到的图片链接。
7. 点击 `Start migration`，上传图片并替换成功项目的链接。
8. 如果存在失败项目，点击 `Retry failed`。

来源域名留空时会处理所有外链图片。所选目标平台自己的域名会被排除，避免重复上传。

### Python CLI

CLI 指在终端中输入命令。下面的命令需要在项目目录中执行，也就是当前目录里应该能
看到 `img_link_migrator.py`。如果终端当前不在项目目录，可以输入 `cd`、空格，
然后把项目文件夹从 Finder 拖入终端，按回车。

#### API key 填在哪里

- **GUI**：先选择上传平台，再把对应密钥粘贴到 API key 输入框。
- **CLI**：使用与 `--provider` 对应的环境变量。

| 上传平台 | CLI 选择方式 | 环境变量 |
| --- | --- | --- |
| PicGo.net / Chevereto | `--provider chevereto`，或者省略 `--provider` | `CHEVERETO_API_KEY` |
| ImgBB | `--provider imgbb` | `IMGBB_API_KEY` |

设置变量的命令取决于当前 shell。先运行 `echo $SHELL`，然后只使用下面与当前 shell
匹配的一组命令，并且只执行所选上传平台对应的输入行。

##### fish

如果终端提示符类似 `目录 (main)>`，并且错误信息中出现
`See help identifiers`，通常正在使用 fish。

1. 复制所选上传平台的 API key。
2. 运行下面的输入命令。
3. 终端显示 `API key:` 后粘贴密钥并按回车。fish 可能用 `*` 遮罩输入。

```fish
# ImgBB
read --silent --global --export --prompt-str 'API key: ' IMGBB_API_KEY

# PicGo.net / Chevereto
read --silent --global --export --prompt-str 'API key: ' CHEVERETO_API_KEY
```

确认所选变量已经设置，不显示密钥内容：

```fish
set --query IMGBB_API_KEY; and echo 'API key 已设置'; or echo 'API key 未设置'
set --query CHEVERETO_API_KEY; and echo 'API key 已设置'; or echo 'API key 未设置'
```

使用结束后清除：

```fish
set --erase --global IMGBB_API_KEY
set --erase --global CHEVERETO_API_KEY
```

##### zsh

```zsh
# ImgBB
read -s "IMGBB_API_KEY?API key: "; echo
export IMGBB_API_KEY

# PicGo.net / Chevereto
read -s "CHEVERETO_API_KEY?API key: "; echo
export CHEVERETO_API_KEY
```

确认已经设置：

```zsh
# 只执行所选上传平台对应的一行。
[[ -n "${IMGBB_API_KEY:-}" ]] && echo "API key 已设置" || echo "API key 未设置"
[[ -n "${CHEVERETO_API_KEY:-}" ]] && echo "API key 已设置" || echo "API key 未设置"
```

使用结束后清除：

```zsh
unset IMGBB_API_KEY
unset CHEVERETO_API_KEY
```

##### bash

```bash
# ImgBB
IFS= read -r -s -p "API key: " IMGBB_API_KEY; echo
export IMGBB_API_KEY

# PicGo.net / Chevereto
IFS= read -r -s -p "API key: " CHEVERETO_API_KEY; echo
export CHEVERETO_API_KEY
```

确认已经设置：

```bash
# 只执行所选上传平台对应的一行。
[[ -n "${IMGBB_API_KEY:-}" ]] && echo "API key 已设置" || echo "API key 未设置"
[[ -n "${CHEVERETO_API_KEY:-}" ]] && echo "API key 已设置" || echo "API key 未设置"
```

使用结束后清除：

```bash
unset IMGBB_API_KEY
unset CHEVERETO_API_KEY
```

设置后，这个终端会话中的迁移命令会自动读取所选平台的 API key。变量只对当前终端
会话有效，关闭该会话后会失效。

API key 只在执行 `--apply` 实际上传时需要。仅扫描和打开 GUI 时不需要提前设置。

也可以直接使用 `--api-key`，但密钥可能进入终端历史或进程列表。下面是以
PicGo.net 为目标的 Chevereto 示例：

```bash
python3 img_link_migrator.py \
  --apply \
  --provider chevereto \
  --api-key "YOUR_CHEVERETO_API_KEY" \
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

第二步，确认扫描结果后执行迁移。下面的默认写法使用 PicGo.net，并读取
`CHEVERETO_API_KEY`：

```bash
python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"
```

如果改为上传到 ImgBB，请设置 `IMGBB_API_KEY` 并显式选择 ImgBB：

```bash
python3 img_link_migrator.py \
  --apply \
  --provider imgbb \
  "$HOME/Documents/MyVault"
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

#### CLI 模式与参数

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
| `--provider NAME` | `NAME` 填 `imgbb` 或 `chevereto`。 | `chevereto`，使用 PicGo.net | `--provider imgbb` 选择 ImgBB。 |
| `--chevereto-url URL` | Chevereto 站点基础 URL。 | `https://www.picgo.net` | 其他站点示例：`--provider chevereto --chevereto-url "https://images.example.com"`。 |
| `--api-key KEY` | 把 `KEY` 换成所选平台的 API key。 | 自动读取 `IMGBB_API_KEY` 或 `CHEVERETO_API_KEY`；缺少时无法迁移。 | `--api-key "YOUR_KEY"`；使用对应环境变量更安全。 |
| `--expiration SECONDS` | 把 `SECONDS` 换成自动删除前的秒数。 | 默认 `0`，请求永久保存。 | 范围 `60`–`15552000`；Chevereto 会收到等价的 ISO 8601 时间段。 |
| `--retries N` | 把 `N` 换成失败后的自动重试次数。 | 默认 `3`，即首次失败后最多再重试 3 次。 | 范围 `0`–`10`；`--retries 0` 表示只尝试一次。 |
| `--include-host DOMAIN` | 把 `DOMAIN` 换成只想处理的来源域名。 | 不限制来源域名，处理所有检测到的外链图片。 | `--include-host xhscdn.com`；可重复使用或用英文逗号分隔。 |
| `--exclude-host DOMAIN` | 把 `DOMAIN` 换成不想处理的来源域名。 | 不增加额外排除项；所选目标平台仍会排除。 | `--exclude-host example.com`；可重复使用或用英文逗号分隔。 |
| `--state-dir PATH` | 把 `PATH` 换成缓存和备份目录。 | 使用对应系统的应用数据目录。 | `--state-dir "./migrator-state"` |
| `--no-backup` | 后面不填值。 | 默认在修改 Markdown 前创建备份。 | 添加后关闭本次备份：`--apply --no-backup "$HOME/Documents/MyVault"`。 |
| `--report PATH` | 把 `PATH` 换成 JSON 报告文件位置。 | 不生成 JSON 报告。 | `--report "./report.json"` |
| `-h`、`--help` | 后面不填值。 | 正常执行命令。 | 显示帮助：`python3 img_link_migrator.py --help`。 |

查看程序生成的命令帮助：

```bash
python3 img_link_migrator.py --help
```

### Python 备份与状态文件

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

持久化缓存会保存源 URL、图片 SHA-256、目标 URL、平台命名空间和过期时间，不会
保存 API key。

### Python 支持的 Markdown 格式

```markdown
![说明文字](https://example.com/image.png)

<img src="https://example.com/image.jpg" alt="示例">

![说明文字][image-id]
[image-id]: https://example.com/image.webp
```

普通链接（例如 `[网站](https://example.com/)`）不会被当作图片处理。

### Python 安全处理流程

完整迁移过程如下：

1. 扫描 Markdown，记录可以替换的图片 URL。
2. 下载并验证第一张源图片。
3. 如果本地存在所选平台仍然有效的缓存链接，则直接复用；否则上传图片。
4. 图片上传完成后，立即处理所有引用该 URL 的 Markdown 文件：
   - 如果启用了备份，并且该文件在本次任务中尚未备份，先备份一次原文件。
   - 确认文件从扫描或上一次程序写入后没有被其他程序修改。
   - 使用原子写入立即替换该图片 URL。
5. 确认写入结束后，才开始下载和上传下一张图片。
6. 重复以上步骤，直到任务完成或取消。

同一文件在一次任务中最多备份一次，备份内容是该文件开始迁移前的状态。正常取消时，
已经上传成功的图片链接已经写入，尚未处理的图片保留原链接。

上传响应返回与本地原子写入之间仍存在极短的程序执行间隔。如果进程在该间隔内被系统
强制终止，所选上传平台中可能会暂时留下一张尚未写入 Markdown 的图片；再次运行时
会复用该平台自己的本地缓存并完成替换。

### Python 的上传自动删除时间

默认值为 `0`，表示请求永久保存。如果希望上传内容自动删除，可以设置
60 到 15,552,000 秒：

```bash
python3 img_link_migrator.py \
  --apply \
  --expiration 600 \
  "$HOME/Documents/MyVault"
```

已经过期的缓存记录不会被复用。临时图片缓存也不会用于需要永久保存的迁移任务。

ImgBB 直接接收秒数；Chevereto 接收等价的 ISO 8601 时间段，例如 `PT600S`。

### Python 开发与测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

检查 Python 语法：

```bash
python3 -m py_compile img_link_migrator.py
```

测试使用模拟的 ImgBB 和 Chevereto 响应，不会实际上传。

### Python 程序限制

- Python GUI/CLI 只修改 UTF-8 编码的 `.md` 和 `.markdown` 文件。
- 需要浏览器登录状态才能访问的源图片可能无法下载。对于小红书 CDN，程序会自动把
  小红书网站设置为 HTTP Referer，这可以处理许多公开图片链接。
- URL 解析主要支持 Obsidian 和 Markdown 的常见图片语法。自定义插件生成的非标准
  语法可能无法识别。
- Python 程序不会删除任何上传平台中的图片。

## macOS 独立单文件工具

`img-link-migrator.command` 是所有程序逻辑都包含在一个文件中的完整 zsh 程序。
可以把它复制或剪切到仓库之外独立运行；运行时不会读取或启动
`img_link_migrator.py`。

### 单文件工具的处理范围

- 接受一个 `.txt`、`.md` 或 `.markdown` 文件，或者一个文件夹。
- 递归搜索文件夹，同时跳过隐藏文件和隐藏目录。
- 直接扫描文件字节，不检测或转换文本编码。
- 查找支持文件中任何位置出现的 `http://` 和 `https://` URL。
- 按来源域名筛选 URL；默认域名是 `xhscdn.com` 及其全部子域名。
- 初次扫描时建立 URL 到文件的索引；上传完成后只检查原本包含该 URL 的文件。
- 同时缓存来源 URL 和下载图片的 SHA-256；已知的相同图片会直接复用目标 URL，
  不再发起上传请求。
- 可以上传到 ImgBB，或者使用 Chevereto API v1 上传到 PicGo.net。
- PicGo.net 的 `Duplicated upload` 响应只要包含有效图片 URL，就按成功复用处理。
- 排除属于当前所选目标平台的现有链接。
- 下载每个选中的 URL，并排除非图片内容以及超过 32 MB 的图片。
- 每次下载和上传最多尝试四次，等待时间自动逐步延长。
- 两个平台的上传请求（包括重试）都限制为每分钟不超过 50 次；遇到限速响应时，
  全部上传统一暂停 60 秒。
- 内部自动重叠最多 3 个下载任务，避免等待来源服务器时浪费上传名额；上传请求仍
  共用一个时间表，程序不再询问 worker 数量。
- 使用该单文件工具自己的持久化来源 URL 缓存。
- 任一任务上传成功后，由主进程串行处理文件，并通过原子写入立即替换索引中的全部
  相同 URL。

单文件扫描器不理解 Markdown 语法结构。在 Markdown 文件中，图片语法、普通文字、
YAML 属性区或代码块内符合来源域名的 URL 都可能被找到；下载后的图片验证会阻止
非图片内容上传。需要 Markdown 语法识别规则时，应使用 Python 程序。

### 单文件工具要求

- macOS
- ImgBB 或 PicGo.net API key
- macOS 系统自带的 `zsh`、`curl`、`plutil`、`file`、`Perl` 及相关命令行工具

### 运行单文件工具

双击 `img-link-migrator.command`，或者在终端运行：

```bash
./img-link-migrator.command
```

程序只会要求填写以下内容：

1. 保留默认的 PicGo.net，或者改选 ImgBB。
2. 输入所选平台的 API key。本次运行中的输入内容会被隐藏。
3. 把一个支持的文件或文件夹拖入终端，然后按回车。
4. 选择来源域名。扫描结束后会立即开始迁移。

上传平台提示如下：

```text
Choose the upload service:
  Press Return or type 1 for PicGo.net (Chevereto API v1).
  Type 2 for ImgBB.
Your choice [1]:
```

| 输入 | 上传目标 |
| --- | --- |
| 直接按回车、`1`、`picgo`、`picgo.net` 或 `chevereto` | PicGo.net |
| `2` 或 `imgbb` | ImgBB |

选择 PicGo.net 时，程序使用 `X-API-Key` 请求头，把图片作为 `source` multipart
字段发送到 `/api/1/upload`。

程序显示的来源说明如下：

```text
Choose where the original image links come from:
  Press Return to use xhscdn.com and its subdomains.
  Or type domains separated by commas: xhscdn.com,example.com
  Or type * to check every domain; non-image URLs are skipped.
Your choice [xhscdn.com]:
```

| 在 `Your choice` 中输入 | 会考虑迁移哪些 URL |
| --- | --- |
| 不输入内容，直接按回车 | `xhscdn.com` 及其全部子域名中的 URL。 |
| `example.com` | `example.com` 及其全部子域名中的 URL。 |
| `xhscdn.com,example.com` | 两个所列域名及其子域名中的 URL。 |
| `*` | 所有域名中的 URL；只有成功下载并验证为图片的内容才会上传。 |

程序不再询问 worker 数量。内部会自动重叠最多 3 个下载任务，但所有 worker 共用
同一个上传时间表。PicGo.net 和 ImgBB 的上传请求开始时间至少间隔 1.21 秒，使速度
保持在每分钟 50 次以下；重试也使用同一时间表。如果收到限速响应，全部上传 worker
会暂停 60 秒，随后自动继续重试。

程序不会再询问是否备份，也不会再询问是否开始。显示扫描数量后，只要存在匹配 URL，
就会立即开始处理。

迁移期间，`Downloading`、`Uploading`、`Uploaded` 和失败信息都会显示该任务的
`[当前编号/总数]`。由于可以同时处理多个任务，完成顺序可能与编号顺序不同。

### 单文件工具的替换安全

单文件工具不建立备份副本。每次文件更新都会先写入同一目录中的临时文件，再通过
原子重命名安装：

1. 源图片必须成功下载并上传，才会进入替换步骤。
2. 文件必须仍与扫描时记录的版本一致。
3. 完整的替换结果必须成功写入临时文件。
4. 满足以上条件后，才会通过原子操作替换原路径。

任何一步失败时，本次替换都不会覆盖该文件；此前已经成功完成的替换会保留在磁盘上。
所有文件修改都由主进程逐个执行，因此并行传输任务不会同时写入同一个文件。

第一次按下 `Control-C` 后，程序会停止派发新任务，等待当前任务组完成，并把
其中成功的结果全部原子写入后再退出。根据自动重试和网络超时情况，安全结束可能需要
等待一段时间。

单文件工具的缓存目录是
`~/Library/Application Support/IMG Link Migrator Standalone/`。`url-map.tsv` 保存
来源 URL 到目标 URL 的对应关系；`content-map.tsv` 保存平台命名空间、SHA-256 和
目标 URL。两个文件都不包含 API key。移动 `.command` 文件不会影响这些缓存。

### 单文件工具示例

以下内容可以位于文本文件或 Markdown 文件中：

```text
图片
------------------------
1. https://sns-webpic-qc.xhscdn.com/path/to/image
```

上传成功后，只会改变 URL：

```text
图片
------------------------
1. https://i.ibb.co/example/image.webp
```

### 单文件工具开发检查

离线自检使用临时 `.txt`、`.md` 和 `.markdown` 文件，其中包含一个不符合 UTF-8
编码的文件。它会在不发起网络请求的情况下验证内部 3 个 worker 队列、内容哈希
复用、重复响应复用、共享上传节流与冷却、安全中断、缓存续传、索引替换和原子写入：

```bash
zsh -n img-link-migrator.command
./img-link-migrator.command --self-test
```

## 许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)。
