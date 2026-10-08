# IMG Link Migrator

[English](README.md)

原生 macOS 图片链接迁移应用：处理文档中的外链图片，上传至 ImgBB 或 PicGo.net，再替换文档链接。应用内置 Python 和图片编解码器，界面、提示、日志、代码注释均为英语。

## 安装

需要 macOS 13 或更新版本。按 Mac 的处理器选择独立安装包：

| 安装包 | 适用设备 |
| --- | --- |
| `IMG-Link-Migrator-1.1.0-arm64.zip` | Apple Silicon，M1 及后续芯片 |
| `IMG-Link-Migrator-1.1.0-x86_64.zip` | Intel Mac |

解压后，将 `IMG Link Migrator.app` 拖进「应用程序」。

## 使用

1. 添加单个或多个 `.txt`、`.md`、`.markdown` 文件，或递归扫描的文件夹，也可拖放。
2. 选择 **ImgBB** 或 **PicGo.net**，在应用界面的 **API key** 输入框填写自己的 key。
3. 选择图片模式和高级选项。默认 **Size Limit**，并开启文档备份。
4. 点击 **Scan**。查看文档、图片链接、数量及来源域名，取消勾选不处理的域名。
5. 点击 **Start Migration**。确认窗口 **Enter 同意、Escape 取消**。
6. 查看状态与图片属性；点击列标题或 **Sort by** 排序。拖动列表下方分隔线调整高度、拖动列边界调整宽度。文字可选择复制，**Copy All** 复制全部，右键复制单行详情。上传中仍可展开高级选项查看。

继续识别行内 Markdown 图片、被图片引用使用的引用式定义、HTML `<img>`。只有 `.txt` 识别裸图片 URL。跳过 frontmatter、围栏代码块及行内代码，保留链接文字和文档结构。

## 全部选项

| 选项 | 默认值／行为 |
| --- | --- |
| Upload to | ImgBB 或 PicGo.net；默认 ImgBB |
| API key | 在应用密码式输入框填写；保存在 macOS 钥匙串 |
| Image mode | Size Limit |
| Back up documents | 开启 |
| Include domains | `xhscdn`，匹配域名中含该关键词的链接；清空即包含全部符合条件的域名；逗号分隔 |
| Exclude domains | 空；逗号分隔；自动排除目标图床域名 |
| Source domain selection | 默认勾选扫描出的所有域名，可取消勾选 |
| 图片 URL | 始终显示 |
| Automatic retries | 3；可设置 0–10 |
| State directory | `~/Library/Application Support/IMG Link Migrator` |
| Clear Cache and Backups | 空闲时将状态目录中的上传缓存和文档备份移到废纸篓 |
| Retry Failed | 重试当前域名选择中的失败图片 |
| Stop | 不再开始新图片，当前操作到安全位置后结束 |

开始迁移前填写所选平台的 key。ImgBB 和 PicGo.net 的 key 分别保存在 macOS 钥匙串，打开应用或切换平台时自动读取。应用记住上次选择的平台；清空输入框会删除该平台保存的 key。后端在活动消息中隐藏当前 key。

## 图片处理规则

**Size Limit，默认模式：**压缩目标小于 **1,000,000 字节**；处理失败则尝试原始文件，由图床决定是否接受。**Original Upload，原图上传模式：**格式支持且未超平台上限时直接使用原文件；超过上限或格式不支持时进入相同处理流程，目标大小改为平台上限。

格式支持且大小达标时使用原文件。需要处理时：

1. 尝试无损 AVIF；普通 8 位图片另尝试无损 WebP，选择达标结果中较小的。
2. 无损不达标，当前尺寸尝试 AVIF **质量 80 → 70**。
3. 两次均超限，当前宽高各缩小 **15%**，新尺寸重新尝试 **80 → 70**。
4. 重复，直到达标或触及终止条件。每个候选结果都从同一份原始解码数据按所需累计比例生成，避免反复压缩上一轮有损结果。

最多尝试 32 个尺寸级别，或较短边达到 16 像素后停止。无法解码、转换失败、动画或无法满足大小目标时，记录提示并上传下载到的原始字节，即使图片已损坏或超出目标大小；不做额外完整解码校验或输出复检。只有下载、上传或文档写入失败时保留原链接。原图下载上限单独设为 100,000,000 字节；需要处理时最多 1 亿像素。大小限制针对图片编码后的文件。

尺寸在缩小步骤前保持原值。AVIF 支持 8、10、12 位及透明通道；没有 NCLX 的普通 8 位 SDR 才尝试 WebP。

- HDR JPEG／HEIC 的 gain map 恢复为 HDR 像素，再编码为 **12 位 BT.2020 / PQ AVIF**；HDR JPEG 达标时原样上传。HEIC gain-map 转换需要 macOS 14 或更新版本。
- 已有 PQ／HLG AVIF 保留原色和传递函数。AVIF gain-map 暂不重编码，无法原样达标时仍尝试上传原文件。
- 主图 NCLX 交由 libavif 正式编码，保留原色、传递函数，包括未指定的 `2/2`；矩阵和范围匹配实际输出。ICC 随支持的处理路径传递，不手工修改编码后的色彩标签。
- 无损 AVIF 使用 RGB identity、4:4:4；有损 AVIF 对 4:2:0 源图使用 4:2:0，其余使用 4:4:4。

## 图床格式和大小

| 平台 | 应用可直接上传的原图格式 | 文件上限 |
| --- | --- | --- |
| ImgBB | JPEG、PNG、BMP、GIF、WebP、AVIF、HEIC／HEIF、TIFF；可解码的 SVG、JPEG 2000、ICO、PSD | 32,000,000 字节 |
| PicGo.net | JPEG、PNG、BMP、GIF、WebP、AVIF | 25,000,000 字节 |

格式通过图片数据识别。上传统一采用无限期保存，关闭自动删除。平台资料：[ImgBB 上传页](https://imgbb.com/)、[ImgBB API](https://api.imgbb.com/)、[PicGo.net 上传页](https://www.picgo.net/)。

## 流程

扫描 → 复用缓存或下载 → 原样上传或压缩 → 处理失败回退原始字节 → 上传成功后备份并替换链接。上传失败保留原链接，可重试。

## 缓存、重试和文档写入

- URL 缓存及处理后内容去重按平台、模式、大小上限和规则版本隔离，复用永久上传记录。
- 并发从 1 按 `1 → 3 → 5 → …` 增长，最多 16 个线程。明确限流／并发拒绝时每次减 1，本轮不再增长；这只是本轮观察到的上限。解码／编码最多同时处理 2 张以控制内存。上传和重试仍共享每分钟最多 50 次的限速器，限流后暂停重试。
- 上传成功或有符合条件的缓存，且文档与扫描或上次成功写入时一致时，替换链接。已上传链接保存在缓存和图片详情中。
- 开启备份时，每次任务首次修改文档前保存原文，用于恢复原始链接和文字。写入采用临时文件及原子替换。
- 停止或退出时等待安全位置，保留已完成结果。修改输入、平台或域名筛选后需要重新扫描。
- 编码器使用临时文件，处理完成后清除。URL／内容映射和文档备份保存在状态目录。点击 **Clear Cache and Backups** 并确认后，将 `state.json` 和 `backups/` 移到废纸篓，可从废纸篓恢复；清理后，后续任务会重新上传匹配的图片。

## 源码构建

需要 macOS、Xcode Command Line Tools（`xcode-select --install`）、开发用 Python 3.11 或更新版本以及网络。Apple Silicon 构建 Intel 包需要 Rosetta 2。Intel Mac 可构建 Intel 包；arm64 包使用 Apple Silicon Mac 构建。

```sh
python3 scripts/build_app.py --arch arm64
python3 scripts/build_app.py --arch x86_64
# 在 Apple Silicon 上构建两个包：
python3 scripts/build_app.py --arch all
```

构建器在 `build/` 下载锁定版本、经过 SHA-256 校验的独立 Python，建立对应架构环境，使用 PyInstaller 打包 pyvips／libvips、Pillow 后备解码器及 pi-heif HEVC 解码组件，从源码构建 libavif／libaom，编译 macOS HDR 辅助程序和 SwiftUI，最终在 `dist/` 生成独立架构 ZIP。

构建器通过 `--sign` 接受 Developer ID 签名身份，通过 `--notary-profile` 接受已配置的 notarytool Keychain profile：

```sh
python3 scripts/build_app.py --arch arm64 --sign 'Developer ID Application: Your Name (TEAMID)' --notary-profile your-profile
```

使用 `--sign` 时，构建器签名应用及内置可执行文件；使用 `--notary-profile` 时，提交 ZIP 公证，给应用附加公证票据后重新生成 ZIP。默认构建使用临时签名。

组件许可随应用保存在 `Contents/Resources/Licenses`。版本、源码及替换重建方法见 [THIRD_PARTY.md](THIRD_PARTY.md)。项目许可：[GNU Affero General Public License v3](LICENSE)。
