# HDU Library Memory

一个非官方的图书馆预约记录纪念报告生成器。用户主动导入自己的预约记录后，项目会生成一份 H5 叙事报告，包含预约次数、累计时长、楼层偏好、期末高峰、毕业祝福和可选的 AI 评价。

## 重要声明

- 本项目不是学校或图书馆官方系统。
- 本项目不应收集统一认证密码。
- 本项目不包含自动预约、抢座、代预约等写入图书馆系统的功能。
- 推荐用户使用导出助手生成 JSON 后导入；Cookie 方式只作为备用方案。
- 如果启用保存或分享，服务器会保存用户提交的预约记录和生成后的报告数据。
- 公开报告链接可能暴露学号、统计数据和生成评价，部署者应提供删除数据或关闭分享的方式。

## 快速启动

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
copy .env.example .env
python web_app.py
```

打开 `http://127.0.0.1:8018`。

## 配置

复制 `.env.example` 为 `.env` 后按需修改：

- `HDULIB_PUBLIC_BASE_URL`：对外访问地址。部署后必须改成你的域名。
- `HDULIB_SIGNING_SECRET`：报告签名密钥。生产环境必须设置为足够长的随机值。
- `HDULIB_AI_API_KEY`：可选。未设置时使用本地兜底评价，不调用大模型。
- `HDULIB_AI_BASE_URL` / `HDULIB_AI_MODEL`：OpenAI-compatible 大模型接口配置。

## 数据流

1. 用户在图书馆页面运行导出助手，或手动上传已导出的 JSON。
2. 服务端聚合预约记录，生成 H5 报告数据。
3. 用户可选择保存档案，使用“学号 + 保存口令”重新生成。
4. 用户可选择分享公开报告，公开报告会带服务端签名，防止被伪造覆盖。

## 开源前检查

发布到 GitHub 前，至少运行：

```bash
gitleaks detect --source .
git status --short
```

如果本机没有安装 gitleaks，也请使用等价的 secret scanner 或人工复核。确认没有 `.env`、`storage/`、真实用户 JSON、Cookie、截图、部署密钥或服务器配置被提交。

## 许可证

默认使用 MIT License。发布前请确认你接受该许可证，或者替换成你需要的许可证。
