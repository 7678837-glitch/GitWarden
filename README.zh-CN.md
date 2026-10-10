# GitWarden

> Mirror the present. Preserve the past.
>
> 镜像当前状态，保全已有历史。

GitWarden 是一个自托管的 Git 历史镜像工具。它按计划把来源仓库 fetch 到本地档案，再把已采集状态推送到一个或多个可写 Git 目标。核心流程不依赖托管平台 API，因此来源和目标可以是任意受支持的 Git URL，也可以手工配置 GitHub 仓库。

[English](README.md) · 简体中文

![GitWarden 总览](docs/images/overview.jpg)

## 它保护什么

每个备份任务包含一个来源、一个持久化本地档案和一个或多个目标。每个目标由两个仓库组成：

- **主镜像仓**：保存当前的 `refs/heads/*` 和 `refs/tags/*`；
- **历史仓**：保存 GitWarden 的 `gw/*` 保全标签，必须设为私有。

当已采集的分支发生 force push 或删除，或者已采集的标签被改写时，GitWarden 会先把旧 Git 对象保存在本地档案，再更新镜像引用；之后先把旧对象推到历史仓，主镜像更新通过门禁后才会继续。普通快进不额外创建保全标签。整个过程自动执行，普通 Git 变更不需要人工审批。

GitWarden 只保护已经采集到的状态。两次轮询之间出现又消失的变更无法恢复。范围包含 Git 对象，不包含 LFS 文件内容和子模块仓库；界面会识别这些引用并将任务标为备份不完整。

历史仓不是第二个完整镜像。它保存被覆盖或删除的历史版本，当前引用仍在主镜像仓和本地档案中。需要最新已采集状态时，请保留主镜像仓或 GitWarden 数据卷。

## 用 Docker Compose 快速启动

准备好本仓库的工作副本后，在项目目录执行：

```bash
cp .env.example .env
```

在 `.env` 中设置必填的管理员密码：

```dotenv
GW_ADMIN_PASSWORD=replace-this-before-start
```

启动服务并打开 <http://127.0.0.1:8080>：

```bash
docker compose up -d --build
docker compose logs -f gitwarden
```

Compose 配置使用项目目录的 `.env`、名为 `gitwarden-data` 的 named volume，以及 `127.0.0.1:8080`。默认镜像在本地构建为 `gitwarden:local`，Compose 会按主机架构自动选择 amd64 或 arm64。容器以 UID/GID `10001` 运行，应用数据库、档案、凭据和临时工作数据都在 `/data` 下。

首次公开发布的镜像只有在 `vYYYYMMDD.N` 发布 tag 通过 CI 后才会推送到 GHCR。在公开 owner/repository 和发布镜像确定前，请使用本地构建。发布后如需使用镜像，可设置 `GW_IMAGE` 并跳过重新构建：

```bash
GW_IMAGE=ghcr.io/sample/gitwarden:20261010.1 docker compose pull
GW_IMAGE=ghcr.io/sample/gitwarden:20261010.1 docker compose up -d --no-build
```

`sample` 只是示例组织，请替换为实际发布镜像路径。

普通 Docker 部署见 [Deployment](docs/wiki/Deployment.md)；配置、数据卷备份和恢复见 [Wiki 源文件索引](docs/wiki/Home.md)。

## 创建第一个备份

1. 使用 `GW_ADMIN_PASSWORD` 登录。WebUI 默认英文，也可以切换简体中文。
2. 打开 **Credentials**，为私有来源或目标保存 HTTPS 用户名/令牌，或者保存 SSH 私钥及对应的 `known_hosts` 条目。只有匿名端点才留空凭据。不要把凭据写进 Git URL。
3. 选择 **Back up a repository**。第 1 步填写来源 URL 和同步间隔；第 2 步填写两个手工目标 URL，或选择已配置的 Gitea/Forgejo 接入自动建目标；第 3 步确认目标安全前提后创建任务。
4. 手动运行一次任务，再检查目标卡片和 **Logs**。目标卡片会显示主镜像是否同步、历史仓已确认的保全记录数量以及上次审计时间。

手工目标的两个仓库在绑定时都必须已经存在且为空。主镜像仓和历史仓都应设为私有，并确保只有 GitWarden 能写入，且推送不会触发 CI 或 webhook。平台建库会创建 `<name>` 和 `<name>-history`，将两者设为私有，关闭 Actions 等仓库功能，并在历史仓保护 `gw/*` 后完成绑定。

![任务详情和历史状态](docs/images/task-detail.jpg)

## 端点和平台扩展

核心接受匿名、HTTPS、SSH、SCP 风格 SSH 和本地绝对路径 Git 端点。HTTPS 凭据通过 Git askpass 传递；SSH 使用临时私钥文件、严格主机密钥校验，操作结束后删除临时文件。

可选平台集成目前只支持 **Gitea** 和 **Forgejo**。它可以发现仓库、按 owner 和通配规则筛选、创建目标、加固目标，并报告当前令牌可见的仓库级/组织级/系统级 webhook 元数据。平台接入必须使用包含用户名和令牌的 HTTPS 凭据；按操作需要，令牌权限应包含 `read:admin`、`write:repository`、`write:organization` 和 `write:user`。

GitHub 没有发现或自动建库适配器。请在三步向导中手工填写 GitHub 来源和两个目标 URL，并遵守目标仓库为空、私有且单一写入方的规则。

## 运行方式

调度器默认使用 2 个 worker，同步间隔下限为 5 分钟，发现间隔下限为 15 分钟，连续 10 分钟无 Git 进度才判定卡死。大仓库只要持续有进度就可以长时间运行；最近运行耗时较长时，间隔会自动延长，连续失败时会退避。

总览页会突出显示备份不完整、目标冲突、门禁阻断、来源可见性变化和逾期任务。**Logs** 保存运行步骤、ref 变化、脱敏后的 Git 输出和管理员操作记录。运行与操作记录默认保留 90 天；保全事件、本地档案和 Git 数据不参与这项清理。

可用 `/health` 做只读的发布检查：

```bash
curl --fail http://127.0.0.1:8080/health
```

响应包含应用版本；如果构建镜像时注入了提交号，也会包含构建 Git commit。

## 文档

- [Wiki 首页](docs/wiki/Home.md)
- [入门](docs/wiki/Getting-started.md)
- [部署](docs/wiki/Deployment.md)
- [配置](docs/wiki/Configuration.md)
- [备份与恢复](docs/wiki/Backup-and-recovery.md)
- [平台接入](docs/wiki/Platforms.md)
- [故障排查](docs/wiki/Troubleshooting.md)

## 许可证

GitWarden 采用 [Apache License 2.0](LICENSE)。第三方依赖保留各自的许可证。

公开 owner/repository 地址仍待确定，发布仓库和镜像链接前需补齐。

[发布验证记录](docs/Validation.md)
