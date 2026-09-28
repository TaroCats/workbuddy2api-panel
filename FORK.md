# FORK.md —— 这个 fork 多了什么

本仓库是 [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)
的 fork。上游代码**一行未改**，只增加了下面这些「fork 专属文件」，用于实现
「自动跟随上游 → 自动出镜像 → 部署机自动更新」。

| 远程 | 地址 | 角色 |
| --- | --- | --- |
| `origin` | `TaroCats/workbuddy2api-panel` | 本 fork，推送到这里 |
| `upstream` | `linguo2625469/workbuddy2api-panel` | 上游源，只读 |

## 全链路

```
上游新提交
  └─ sync-upstream.yml   每天两次检测 → rebase 到上游之上 → push fork/main
       └─ docker-ghcr.yml  push 到 main 触发 → 构建 :latest（纯上游，上游文件未改）
            └─ docker-fork.yml  上一步构建成功后 → 叠一层 → 构建 :latest-fork
                 └─ 部署机容器内 selfupdate.py  每 6 小时按 digest 比对 → 原地换容器
```

镜像分层（同一个 GHCR 包，两个 tag）：

| tag | 内容 | 谁构建 |
| --- | --- | --- |
| `:latest`（以及 `:main`、`:sha-xxx`） | 纯上游：`/app/wb2api` + `login.sh` 等 | `docker-ghcr.yml`（上游自带，未改） |
| **`:latest-fork`**（以及 `:sha-xxx-fork`） | 上面那些 **+ 自更新脚本 + 入口包装** | `Dockerfile.fork` + `docker-fork.yml`（fork 独有） |

**部署机 pull 的是 `:latest-fork`**。`:latest` 里没有自更新脚本，别混用。

## 1. 基础镜像：`.github/workflows/docker-ghcr.yml`（上游自带，未改）

`IMAGE_NAME: ${{ github.repository }}` 在 fork 里解析成 `ghcr.io/TaroCats/...`，
`docker/metadata-action` 会自动转小写，所以实际产物是 **`ghcr.io/tarocats/workbuddy2api-panel`**。

**刻意不改这个文件**：保持原样，上游以后改进它（换 action 版本、加平台等）能原样同步过来。
自己再写一个只会分叉。

## 2. 部署镜像：`Dockerfile.fork` + `.github/workflows/docker-fork.yml`（fork 独有）

干的事只有一件：在 `:latest` 之上把两个脚本焊进镜像，产出 `:latest-fork`。

为什么非要叠这一层 —— 上游 Dockerfile 里没有 fork 的脚本，而 compose 的**短语法 bind mount
在宿主机源文件缺失时会「默默把宿主机上那个路径建成目录」**（[compose 规范](https://compose-spec.github.io/compose-spec/spec.html)
明文行为，为兼容旧版 docker-compose 而保留）。目录挂进容器后，`bash` 拿到的是目录，报：

```
/app/selfupdate-entrypoint.sh: /app/selfupdate-entrypoint.sh: Is a directory
```

脚本焊进镜像后，**宿主机上不需要存在任何脚本文件**，这类失败从根上消失。

触发方式：`docker-ghcr` 构建成功（`workflow_run`，PR 触发和失败/取消的都不算）或手动派发
`docker-fork`（可带 `base_tag` 指定要叠的基础镜像 tag）。

为什么不在这里顺手派发一次基础构建：那会把「上一次的 `:latest`」焊进 `:latest-fork`。
宁可等基础构建跑完 5 分钟，由完成事件驱动。

## 3. 跟上游：`.github/workflows/sync-upstream.yml`（fork 独有）

每天北京时间 11:37、23:37 跑一次（`workflow_dispatch` 可手动触发，带 `force` 开关），流程：

1. `git fetch upstream main`，数一下 fork 落后 / 领先多少个提交
2. **没落后** → 直接收工，不提交、不推送（所以不会每 12 小时攒一个空提交）
3. **落后且 fork 无本地提交** → `git merge --ff-only`，真·快进，普通 push
4. **落后且 fork 有本地提交**（正常情况，本 fork 至少带一个提交）→ `git rebase upstream/main`
   把本地提交线性重放到上游之上，然后 `--force-with-lease` 推送
   - 结果是**没有 merge 提交**，fork 专属文件原样保留
   - **冲突就中止**：`git rebase --abort` + 任务红灯，宁可不更新也不推半成品
5. 推送成功后显式派发 `docker-ghcr.yml` 重新构建（后面的叠层由 `docker-fork.yml` 自动接上）

为什么推送后还要显式派发：由 `GITHUB_TOKEN` 产生的 push **不会**触发其他 workflow
（GitHub 的防递归限制），只有 `workflow_dispatch` / `repository_dispatch` 是例外。

为什么不是「硬重置完全镜像」：那样会把 fork 独有的文件（含这个 workflow 自己）删掉，
必须额外维护一份覆盖层回填清单。rebase 天然保留本地提交，不需要清单。

> 新增 fork 专属文件后，记得加进 `sync-upstream.yml` 里的 `FORK_FILES` 清单 ——
> 那个清单是**存在性断言**，不是回填清单：文件被误删时能让任务立刻红灯，而不是悄悄失效。

## 4. 自更新：`docker-compose.fork.yml` + `scripts/selfupdate*.sh|py`（fork 独有）

容器自己检查 `:latest-fork` 有没有新 digest，有就把自己原地换成新镜像，不需要宿主机 cron，
也不需要 watchtower。脚本在镜像里，**不要在宿主机上另存一份**。

**部署（注意不要和基础 compose 叠加）**：

```bash
cp config.example.json config.json
docker compose -f docker-compose.fork.yml pull
docker compose -f docker-compose.fork.yml up -d
```

不要写 `-f docker-compose.yml -f docker-compose.fork.yml`：服务会同时带 `build:` 和
`image:`，compose 会走本地源码构建，拉镜像那条路就白搭了。

### 为什么需要一个「调度容器」

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

### 开关（都在 `docker-compose.fork.yml` 的 environment 里）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SELF_UPDATE` | `1` | `0` 关闭自更新（回到纯手动 `docker compose pull`） |
| `SELF_UPDATE_IMAGE` | `ghcr.io/tarocats/workbuddy2api-panel:latest-fork` | 跟随的镜像 |
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

### 关于 `user: root`

基础 compose 默认以 uid 10001 运行，这里默认 root —— 因为自更新要写 `docker.sock`。
**挂 `docker.sock` 本身就等于把宿主机 root 权限交给这个容器**（能起特权容器、能挂宿主机根目录），
既然已经交了 root，再把容器内用户设成 10001 只是心理安慰。

不接受这个风险的话有两条路：把 `SELF_UPDATE=0`（不挂 socket，纯手动更新），
或者保留非 root 用户并加 `group_add` 让它进 docker 组。

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/app/selfupdate-entrypoint.sh: Is a directory` | 部署目录里的 `scripts/selfupdate-entrypoint.sh`（或 `.py`）**不存在**，被老版 compose 的短语法在宿主机上建成了空目录，又被挂进容器 | 现在的 compose 已不再挂这两个脚本，`git pull` 到最新 `docker-compose.fork.yml` 后 `docker compose -f docker-compose.fork.yml up -d --force-recreate` 即可。宿主机上遗留的空目录可以顺手清掉：`rmdir scripts/selfupdate-entrypoint.sh scripts/selfupdate.py scripts 2>/dev/null`（`rmdir` 只删空目录，里面真有东西会拒绝，安全） |
| `/app/selfupdate-entrypoint.sh: No such file or directory` | 用成了纯上游镜像 `:latest`（里面没有自更新脚本） | 把 `image:` / `WB2API_IMAGE` / `SELF_UPDATE_IMAGE` 都指到 `:latest-fork` |
| `bind source path does not exist: .../config.json` | 宿主机还没 `cp config.example.json config.json`（compose 长语法 + `create_host_path: false`，刻意让它报错而不是建目录） | 补上 `cp` 再 `up -d` |
| 容器起来了但从不更新 | 没挂 `/var/run/docker.sock`、或 `SELF_UPDATE=0` | 看 `docker logs workbuddy2api` 开头有没有 `[selfupdate]` 的提示行 |
| `:latest-fork` 一直是旧的 | `docker-fork` 没跑或跑失败（它在基础构建成功后才触发） | 到 Actions 看基础构建是否成功；手动 `gh workflow run docker-fork.yml` 兜底 |

## 需要注意的

- **GHCR 包可见性**：仓库是 public，所以推上去的包默认也是 public，宿主机直接 pull 即可，
  不用去改任何设置。只有当 fork 变成 private 仓库时才需要把包改成 Public，或者
  `docker login ghcr.io`。
- **fork 里上游自带的 workflow 可能是禁用状态**（`disabled_fork`）：被禁用的 workflow
  无法被派发，会在同步任务里报 `403`/`422`。到 Actions 页面启用一次即可。
- **`main` 开了分支保护**的话，要允许 force push，否则第 4 种同步路径会失败。
- 上游改了 `.github/workflows/sync-upstream.yml` 这个路径（同名文件）会导致 rebase 冲突。
  真遇上了：同步任务会红灯并附说明，人工 rebase 一次即可。
