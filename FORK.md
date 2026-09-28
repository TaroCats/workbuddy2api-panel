# FORK.md —— 这个 fork 多了什么

本仓库是 [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)
的 fork。上游代码**一行未改**，只增加了下面这些「fork 专属文件」，用于实现
「自动跟随上游 → 自动出镜像 → 部署机自动更新」。

| 远程 | 地址 | 角色 |
| --- | --- | --- |
| `origin` | `TaroCats/workbuddy2api-panel` | 本 fork，推送到这里 |
| `upstream` | `linguo2625469/workbuddy2api-panel` | 上游源，只读 |

## 三件套

### 1. 出镜像：`.github/workflows/docker-ghcr.yml`（上游自带，未改）

上游已经写好了多架构构建并推送 GHCR 的 workflow。里面的 `IMAGE_NAME: ${{ github.repository }}`
在 fork 里会解析成 `ghcr.io/TaroCats/workbuddy2api-panel`，而 `docker/metadata-action`
会自动把镜像名转成全小写，所以实际产物是 **`ghcr.io/tarocats/workbuddy2api-panel`**。

**刻意不改这个文件**：保持原样，上游以后改进它（换 action 版本、加平台等）能原样同步过来。
自己再写一个只会分叉。

触发条件：push `main` / 打 `v*` tag / 手动触发 / 被下面的同步 workflow 派发。

### 2. 跟上游：`.github/workflows/sync-upstream.yml`（fork 独有）

每天北京时间 11:37、23:37 跑一次（`workflow_dispatch` 可手动触发，带 `force` 开关），流程：

1. `git fetch upstream main`，数一下 fork 落后 / 领先多少个提交
2. **没落后** → 直接收工，不提交、不推送（所以不会每 12 小时攒一个空提交）
3. **落后且 fork 无本地提交** → `git merge --ff-only`，真·快进，普通 push
4. **落后且 fork 有本地提交**（正常情况，本 fork 至少带一个提交）→ `git rebase upstream/main`
   把本地提交线性重放到上游之上，然后 `--force-with-lease` 推送
   - 结果是**没有 merge 提交**，fork 专属文件原样保留
   - **冲突就中止**：`git rebase --abort` + 任务红灯，宁可不更新也不推半成品
5. 推送成功后显式派发 `docker-ghcr.yml` 重新构建

为什么推送后还要显式派发：由 `GITHUB_TOKEN` 产生的 push **不会**触发其他 workflow
（GitHub 的防递归限制），只有 `workflow_dispatch` / `repository_dispatch` 是例外。

为什么不是「硬重置完全镜像」：那样会把 fork 独有的文件（含这个 workflow 自己）删掉，
必须额外维护一份覆盖层回填清单。rebase 天然保留本地提交，不需要清单。

> 新增 fork 专属文件后，记得加进 `sync-upstream.yml` 里的 `FORK_FILES` 清单 ——
> 那个清单是**存在性断言**，不是回填清单：文件被误删时能让任务立刻红灯，而不是悄悄失效。

### 3. 自更新：`docker-compose.fork.yml` + `scripts/selfupdate*.sh|py`（fork 独有）

容器自己检查 GHCR 上有没有新镜像，有就把自己原地换成新镜像，不需要宿主机 cron，
也不需要 watchtower。

**部署（注意不要和基础 compose 叠加）**：

```bash
cp config.example.json config.json
docker compose -f docker-compose.fork.yml pull
docker compose -f docker-compose.fork.yml up -d
```

不要写 `-f docker-compose.yml -f docker-compose.fork.yml`：服务会同时带 `build:` 和
`image:`，compose 会走本地源码构建，拉镜像那条路就白搭了。

#### 为什么需要一个「调度容器」

自更新最大的坑是**新旧容器争抢宿主机端口 7863**：

- 先停掉自己 → 就再没有存活的进程能去启动新容器
- 不停自己直接起新容器 → 新容器因端口被占用起不来

所以交割必须交给一个**在本容器之外**的进程。`selfupdate.py --once` 的实际流程：

```
查 Registry digest（/distribution，不下载）→ 与本地相同就收工
  ↓ 不同
docker pull → 比对镜像 ID（最终判据）
  ↓ 真的变了
用临时名（workbuddy2api-new-<ts>）创建新容器，**不启动**
  ↓            ← 这一步失败的话，旧容器完全没被动过，业务无感
用【旧镜像】创建并启动调度容器（NetworkMode=none，不碰端口）
  ↓
调度容器接手：停旧容器 → 新容器改回正式名 → 启动 → 确认 running → 删旧容器 → 自删
```

几个刻意的设计：

- **调度容器用旧镜像创建**：旧镜像一定含 python3、一定是已验证可用的。哪怕新镜像本身是坏的，
  交割逻辑依然跑得起来，也依然回滚得回去。
- **启动后先等 5 秒再确认**：抓「起来就崩」的镜像，崩了就把旧容器启回来、名字改回去。
- **残留自清理**：上次更新跑一半留下的 `*-new-*` / `*-old-*` / `*-swap-*` 容器，
  下一轮启动时会自动清掉（只清已停止的，不动正在跑的，也不动别的项目）。

#### 开关（都在 `docker-compose.fork.yml` 的 environment 里）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SELF_UPDATE` | `1` | `0` 关闭自更新（回到纯手动 `docker compose pull`） |
| `SELF_UPDATE_IMAGE` | `ghcr.io/tarocats/workbuddy2api-panel:latest` | 跟随的镜像 |
| `SELF_UPDATE_INTERVAL` | `21600` | 检查间隔秒（6 小时） |
| `SELF_UPDATE_JITTER` | `600` | 每次额外随机抖动秒，避免多台机器同刻拉取 |
| `SELF_UPDATE_PRUNE` | `0` | `1` = 更新成功后清理 dangling 旧镜像（默认留一份好回滚） |
| `SELF_UPDATE_USERNAME` / `SELF_UPDATE_TOKEN` | 空 | 只有私有包才需要 |

手动排查（都在容器内）：

```bash
docker exec workbuddy2api python3 /app/scripts/selfupdate.py --check      # 只看有没有更新
docker exec workbuddy2api python3 /app/scripts/selfupdate.py --dry-run    # 走完流程但不替换
docker logs workbuddy2api | grep selfupdate                               # 看历史交割记录
```

`--check` 退出码：`0` 已最新 / `10` 有更新 / `1` 无法判定。

#### 关于 `user: root`

基础 compose 默认以 uid 10001 运行，这里默认 root —— 因为自更新要写 `docker.sock`。
**挂 `docker.sock` 本身就等于把宿主机 root 权限交给这个容器**（能起特权容器、能挂宿主机根目录），
既然已经交了 root，再把容器内用户设成 10001 只是心理安慰。

不接受这个风险的话有两条路：把 `SELF_UPDATE=0`（不挂 socket，纯手动更新），
或者保留非 root 用户并加 `group_add` 让它进 docker 组。

## 需要注意的

- **GHCR 包可见性**：仓库是 public，所以推上去的包默认也是 public，宿主机直接 pull 即可，
  不用去改任何设置。只有当 fork 变成 private 仓库时才需要把包改成 Public，或者
  `docker login ghcr.io`。
- **fork 里上游自带的 workflow 可能是禁用状态**（`disabled_fork`）：被禁用的 workflow
  无法被派发，会在同步任务里报 `403`/`422`。到 Actions 页面启用一次即可。
- **`main` 开了分支保护**的话，要允许 force push，否则第 4 种同步路径会失败。
- 上游改了 `.github/workflows/sync-upstream.yml` 这个路径（同名文件）会导致 rebase 冲突。
  真遇上了：同步任务会红灯并附说明，人工 rebase 一次即可。
