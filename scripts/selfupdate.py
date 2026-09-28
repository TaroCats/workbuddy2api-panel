#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb2api 容器内自更新（fork 专属，上游没有这个文件）。

只用 Python 标准库，直连挂载进来的 /var/run/docker.sock 调 Docker Engine API，
检查 GHCR 上是否有比当前容器更新的镜像；有则把容器原地替换成新镜像。

为什么这么绕：本脚本跑在**要被替换的那个容器里**，而且新容器要绑同一个宿主机端口
（7863）。所以「停掉自己 → 起新容器」这条路走不通：
  · 停掉自己，就再没有存活的进程能去启动新容器；
  · 不停自己直接起新容器，新容器会因为端口被占用而启动失败。
因此交割必须交给一个**在本容器之外**的进程。流程是：

  1. 先查 Registry 上 latest 的 digest（/distribution 接口，不下载）与本地比对，相同就收工
  2. 不同 → docker pull 拉新镜像，再比对镜像 ID（最终判据）
  3. 用**临时名**创建新容器（不启动）—— 不碰还在服务中的旧容器，失败也不影响业务
  4. 用**旧镜像**创建一个「调度容器」（Swap）并启动它，它挂在 docker.sock 上、用自己的
     NetworkMode=none 所以不占端口
  5. 调度容器接手：停旧容器 → 新容器改回正式名 → 启动新容器 → 确认 running → 删旧容器

  调度容器用旧镜像创建（旧镜像一定含 python3，是已验证可用的），因此即使新镜像本身是坏的，
  交割逻辑依然能跑，也依然能回滚。任何一步失败都会把旧容器启回来、名字改回去。

用法：
  selfupdate.py --loop                 # 常驻守护（compose 里默认用这个）
  selfupdate.py --once                 # 检查并更新一次
  selfupdate.py --check                # 只判断有无更新：退出码 0=已最新 10=有更新 1=无法判定
  selfupdate.py --dry-run              # 走完检查和拉取，但不创建/替换容器
  selfupdate.py --swap --old A --new B --name workbuddy2api
                                       # 内部使用：调度容器执行交割

环境变量：
  SELF_UPDATE_IMAGE      目标镜像，默认 ghcr.io/tarocats/workbuddy2api-panel:latest
  SELF_UPDATE_INTERVAL   循环间隔秒，默认 21600（6 小时）
  SELF_UPDATE_JITTER     每次额外随机抖动上限秒，默认 600（避免所有机器同时拉）
  SELF_UPDATE_SWAP_DELAY 调度容器动手前等待秒，默认 5（留时间让日志落盘）
  SELF_UPDATE_START_RETRIES / SELF_UPDATE_START_RETRY_DELAY
                         启动新容器的重试次数 / 间隔秒，默认 15 / 2
                         （旧容器停止后宿主机端口释放可能有一两秒延迟）
  SELF_UPDATE_PRUNE      1 = 更新成功后清理 dangling 镜像（默认 0，留回滚余地）
  SELF_UPDATE_CONTAINER  显式指定自身容器 id/名字（自动识别失败时的逃生口）
  SELF_UPDATE_USERNAME / SELF_UPDATE_TOKEN   私有包时的仓库凭据
  DOCKER_SOCK            docker socket 路径，默认 /var/run/docker.sock
"""

import base64
import json
import os
import random
import re
import signal
import socket
import sys
import time
import urllib.parse

DEFAULT_IMAGE = "ghcr.io/tarocats/workbuddy2api-panel:latest"
SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# 退出码
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10


def log(msg):
    print("[selfupdate] %s" % msg, flush=True)


def short_id(x):
    return (x or "?")[:19]


class DockerError(RuntimeError):
    def __init__(self, status, body):
        body = body if isinstance(body, str) else body.decode("utf-8", "replace")
        super().__init__("HTTP %s: %s" % (status, body[:400]))
        self.status = status
        self.body = body


def _dechunk(data):
    out = bytearray()
    while True:
        idx = data.find(b"\r\n")
        if idx < 0:
            break
        try:
            size = int(data[:idx].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += data[idx + 2: idx + 2 + size]
        data = data[idx + 2 + size + 2:]
    return bytes(out)


class Docker(object):
    """极小的 Docker Engine API 客户端（unix socket + HTTP/1.1）。"""

    def __init__(self, sock_path, timeout=120):
        self.sock_path = sock_path
        self.timeout = timeout
        self.api_version = None

    # ---------- 底层 ----------

    def _send(self, method, path, body, headers, timeout):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock_path)
        except FileNotFoundError:
            raise DockerError(0, "docker socket 不存在：%s" % self.sock_path)
        except PermissionError:
            raise DockerError(0, "无权访问 docker socket：%s（把容器跑成 root，或让容器用户能读该 socket）" % self.sock_path)
        except OSError as e:
            raise DockerError(0, "连接 docker socket 失败：%s" % e)

        hdrs = {"Host": "docker", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        payload = b""
        if body is not None:
            payload = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))

        req = ("%s %s HTTP/1.1\r\n" % (method, path)).encode()
        for k, v in hdrs.items():
            req += ("%s: %s\r\n" % (k, v)).encode()
        req += b"Connection: close\r\n\r\n"

        try:
            s.sendall(req + payload)
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        except socket.timeout:
            raise DockerError(0, "请求 docker socket 超时：%s %s" % (method, path))
        finally:
            s.close()

        raw = b"".join(chunks)
        head, _, rest = raw.partition(b"\r\n\r\n")
        if not head:
            raise DockerError(0, "docker socket 返回空响应（daemon 未就绪？）")
        lines = head.split(b"\r\n")
        try:
            status = int(lines[0].split(b" ")[1])
        except (IndexError, ValueError):
            raise DockerError(0, "无法解析响应状态行：%r" % lines[0][:120])
        hdr = {}
        for line in lines[1:]:
            k, _, v = line.partition(b":")
            hdr[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
        if hdr.get("transfer-encoding", "").lower() == "chunked":
            rest = _dechunk(rest)
        return status, hdr, rest

    def _full_path(self, path):
        if self.api_version and not path.startswith("/v"):
            return "/v%s%s" % (self.api_version, path)
        return path

    def call(self, method, path, body=None, headers=None, timeout=None, ok=(200, 201, 204)):
        status, _, data = self._send(method, self._full_path(path), body, headers,
                                     timeout or self.timeout)
        if status not in ok:
            raise DockerError(status, data)
        return data

    def get_json(self, path, timeout=None):
        data = self.call("GET", path, timeout=timeout)
        if not data:
            return {}
        return json.loads(data.decode("utf-8", "replace"))

    def post(self, path, body=None, timeout=None, ok=(200, 201, 204)):
        return self.call("POST", path, body=body, timeout=timeout, ok=ok)

    def delete(self, path, timeout=None, ok=(200, 204, 404)):
        return self.call("DELETE", path, timeout=timeout, ok=ok)

    # ---------- 高层 ----------

    def connect(self):
        status, _, data = self._send("GET", "/version", None, None, 10)
        if status != 200:
            raise DockerError(status, data)
        ver = json.loads(data.decode("utf-8", "replace"))
        self.api_version = ver.get("ApiVersion")
        return ver


# ---------------------------------------------------------------- 镜像工具

def split_image_ref(ref):
    """'ghcr.io/a/b:tag' -> ('ghcr.io/a/b', 'tag')；无 tag 补 latest。"""
    if "@" in ref:
        return ref, None
    head, sep, tail = ref.rpartition("/")
    if sep and ":" in tail:
        name, _, tag = tail.partition(":")
        return head + "/" + name, tag
    if not sep and ":" in tail:
        name, _, tag = tail.partition(":")
        return name, tag
    return ref, "latest"


def registry_auth_header():
    if os.environ.get("SELF_UPDATE_AUTH"):
        return os.environ["SELF_UPDATE_AUTH"]
    user = os.environ.get("SELF_UPDATE_USERNAME")
    pwd = os.environ.get("SELF_UPDATE_TOKEN")
    if user and pwd:
        blob = json.dumps({"username": user, "password": pwd}).encode()
        return base64.b64encode(blob).decode()
    return None


def remote_digest(d, image_name, tag):
    """查 Registry 上该 tag 的 digest（不下载镜像）。拿不到返回 None。"""
    path = "/distribution/%s/json" % image_name
    if tag:
        path += "?tag=" + urllib.parse.quote(tag, safe="")
    try:
        data = d.get_json(path, timeout=60)
    except DockerError as e:
        log("distribution 查询不可用（%s）——退化为 pull 后比对镜像 ID" % e)
        return None
    desc = (data or {}).get("Descriptor") or {}
    digest = desc.get("digest") or ""
    return digest if digest.startswith("sha256:") else None


def local_repo_digests(d, image_id, image_name):
    try:
        img = d.get_json("/images/%s/json" % image_id)
    except DockerError:
        return set()
    out = set()
    for rd in img.get("RepoDigests") or []:
        repo, _, digest = rd.partition("@")
        if not digest:
            continue
        if image_name.endswith(repo) or repo.endswith(image_name):
            out.add(digest)
    return out


def pull_image(d, image_name, tag):
    path = "/images/create?fromImage=" + urllib.parse.quote(image_name, safe="")
    if tag:
        path += "&tag=" + urllib.parse.quote(tag, safe="")
    headers = {}
    auth = registry_auth_header()
    if auth:
        headers["X-Registry-Auth"] = auth
    data = d.call("POST", path, headers=headers, timeout=1800, ok=(200,))
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        err = obj.get("error") or obj.get("errorDetail")
        if err:
            raise DockerError(0, "拉取镜像失败：%s" % (err if isinstance(err, str) else json.dumps(err)))


# ---------------------------------------------------------------- 自身定位

def self_container_id(d, override=None):
    if override:
        d.get_json("/containers/%s/json" % override)  # 校验存在，不存在会抛错
        return override

    candidates = []
    host = socket.gethostname()
    if host:
        candidates.append(host)

    for path in ("/proc/self/cgroup", "/proc/self/mountinfo"):
        try:
            with open(path) as f:
                txt = f.read()
        except OSError:
            continue
        for m in re.finditer(r"([0-9a-f]{12,64})", txt):
            candidates.append(m.group(1))
        if len(candidates) > 8:
            break

    for c in candidates:
        try:
            d.get_json("/containers/%s/json" % c)
            return c
        except DockerError:
            continue
    raise DockerError(0, "无法定位自身容器（hostname=%s）。可用 SELF_UPDATE_CONTAINER 显式指定。" % host)


def wait_running(d, cid, attempts=30, delay=1.0):
    for _ in range(attempts):
        try:
            st = d.get_json("/containers/%s/json" % cid)
        except DockerError:
            time.sleep(delay)
            continue
        state = st.get("State") or {}
        if state.get("Running"):
            return True
        if state.get("Status") in ("exited", "dead"):
            return False
        time.sleep(delay)
    return False


def cleanup_orphans(d, base_name, dry_run=False):
    """清掉上一次更新留下的、已停止的临时容器（*-new-* / *-old-* / *-swap-*）。"""
    prefixes = tuple("%s-%s-" % (base_name, kind) for kind in ("new", "old", "swap"))
    flt = json.dumps({"name": [base_name]})
    try:
        items = d.get_json("/containers/json?all=1&filters=%s" % urllib.parse.quote(flt, safe=""))
    except DockerError as e:
        log("查询残留容器失败（忽略）：%s" % e)
        return
    for it in items:
        nm = (it.get("Names") or [""])[0].lstrip("/")
        if not nm.startswith(prefixes):
            continue
        if it.get("State") == "running":
            continue
        if dry_run:
            log("dry-run：将清理残留容器 %s" % nm)
            continue
        log("清理残留容器 %s" % nm)
        try:
            d.delete("/containers/%s?force=1&v=1" % it["Id"])
        except DockerError as e:
            log("清理 %s 失败（忽略）：%s" % (nm, e))


# ---------------------------------------------------------------- 交割（调度容器内执行）

def do_swap(old_id, new_id, final_name, dry_run=False):
    """由调度容器调用。幂等：任何一步重跑都不会把系统弄坏。"""
    d = Docker(SOCK)
    d.connect()

    delay = int(os.environ.get("SELF_UPDATE_SWAP_DELAY", "5"))
    if delay > 0:
        log("调度容器就绪，%d 秒后开始交割（等旧容器把日志写完）" % delay)
        time.sleep(delay)

    def state(cid):
        try:
            return (d.get_json("/containers/%s/json" % cid).get("State") or {})
        except DockerError:
            return {}

    def rolled_back_ok():
        """回滚：先清掉新容器，再把旧容器启回来、名字改回去。"""
        log("回滚：删除新容器 %s" % new_id[:12])
        try:
            d.delete("/containers/%s?force=1&v=1" % new_id)
        except DockerError as e:
            log("删除新容器失败（忽略）：%s" % e)
        log("回滚：重启旧容器 %s" % old_id[:12])
        try:
            d.post("/containers/%s/start" % old_id, ok=(204, 304, 404))
        except DockerError as e:
            log("重启旧容器失败（请手动 docker start）：%s" % e)
        try:
            d.post("/containers/%s/rename?name=%s" % (old_id, urllib.parse.quote(final_name, safe="")),
                   ok=(204, 404))
        except DockerError as e:
            log("改名回滚失败（忽略）：%s" % e)

    # 1) 新容器已经在跑了 → 说明是重跑，直接收尾
    if state(new_id).get("Running"):
        log("新容器 %s 已在运行，直接收尾" % new_id[:12])
    else:
        # 2) 停旧容器（腾出宿主机端口）
        if state(old_id).get("Running"):
            log("停止旧容器 %s" % old_id[:12])
            try:
                d.post("/containers/%s/stop?t=30" % old_id, timeout=120, ok=(204, 304, 404))
            except DockerError as e:
                log("停止旧容器报错（继续尝试启动新的）：%s" % e)

        # 3) 新容器改回正式名（临时名创建是为了在旧容器还活着时不冲突）
        try:
            d.post("/containers/%s/rename?name=%s" % (new_id, urllib.parse.quote(final_name, safe="")),
                   ok=(204,))
            log("新容器已改名为 %s" % final_name)
        except DockerError as e:
            log("改名失败：%s" % e)
            rolled_back_ok()
            return EXIT_ERROR

        # 4) 启动新容器（端口释放可能有一两秒延迟，重试）
        retries = int(os.environ.get("SELF_UPDATE_START_RETRIES", "15"))
        retry_delay = float(os.environ.get("SELF_UPDATE_START_RETRY_DELAY", "2"))
        started = False
        for attempt in range(1, retries + 1):
            try:
                d.post("/containers/%s/start" % new_id, ok=(204, 304))
                started = True
                break
            except DockerError as e:
                log("第 %d 次启动新容器失败：%s" % (attempt, e))
                time.sleep(retry_delay)
        if not started:
            log("新容器启动失败，回滚")
            rolled_back_ok()
            return EXIT_ERROR

        # 5) 确认真的活着（防止起来就崩的镜像）
        if not wait_running(d, new_id, attempts=15, delay=1.0):
            log("新容器未进入 running，回滚")
            rolled_back_ok()
            return EXIT_ERROR
        # 再稳一下，抓「起来又立刻退出」的崩循环
        time.sleep(5)
        if not state(new_id).get("Running"):
            log("新容器启动后立即退出（crash loop），回滚")
            rolled_back_ok()
            return EXIT_ERROR

    # 6) 回收旧容器
    log("交割完成：新容器 %s 已接管，删除旧容器 %s" % (new_id[:12], old_id[:12]))
    try:
        d.delete("/containers/%s?force=1&v=1" % old_id)
    except DockerError as e:
        log("删除旧容器失败（可手动 docker rm）：%s" % e)

    if os.environ.get("SELF_UPDATE_PRUNE") == "1":
        try:
            d.post("/images/prune?filters=%s"
                   % urllib.parse.quote(json.dumps({"dangling": ["true"]}), safe=""), ok=(200,))
            log("已清理 dangling 镜像")
        except DockerError as e:
            log("清理 dangling 镜像失败（忽略）：%s" % e)

    # 7) 调度容器删掉自己（AutoRemove 会失败于 restart 策略，所以自己来）
    try:
        me = self_container_id(d, os.environ.get("SELF_UPDATE_CONTAINER"))
        d.delete("/containers/%s?force=1&v=1" % me, timeout=5)
    except Exception as e:
        log("调度容器自删未确认（属正常，Docker 会在退出后清理）：%s" % e)
    return EXIT_OK


# ---------------------------------------------------------------- 主流程

def build_create_payload(info, image_ref):
    cfg = info.get("Config") or {}
    host = dict(info.get("HostConfig") or {})
    cid = info.get("Id") or ""

    # hostname：默认是容器 id 的前 12 位（self_container_id 依赖这一点，必须重新生成）。
    # 只有用户显式设过别的 hostname 才沿用。
    hostname = cfg.get("Hostname") or ""
    if hostname and hostname not in (cid, cid[:12]):
        new_hostname = hostname
    else:
        new_hostname = ""

    payload = {
        "Image": image_ref,
        "Env": cfg.get("Env"),
        "Cmd": cfg.get("Cmd"),
        "Entrypoint": cfg.get("Entrypoint"),
        "WorkingDir": cfg.get("WorkingDir") or "",
        "User": cfg.get("User") or "",
        "Labels": dict(cfg.get("Labels") or {}),
        "ExposedPorts": cfg.get("ExposedPorts"),
        "Healthcheck": cfg.get("Healthcheck"),
        "StopSignal": cfg.get("StopSignal"),
        "Hostname": new_hostname,
        "AttachStdin": False,
        "AttachStdout": False,
        "AttachStderr": False,
        "Tty": False,
        "OpenStdin": False,
        "StdinOnce": False,
        "HostConfig": host,
    }
    # 网络别名也要带走：compose 会给容器注册服务名别名，丢了可能影响容器间用服务名互访
    nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
    if nets:
        endpoints = {}
        for net_name, net in nets.items():
            ep = {}
            aliases = (net or {}).get("Aliases")
            if aliases:
                ep["Aliases"] = aliases
            endpoints[net_name] = ep
        payload["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return {k: v for k, v in payload.items() if v is not None}


def schedule_swap(d, info, old_id, new_id, base_name, dry_run=False):
    """用旧镜像建一个调度容器，让它在本容器之外完成交割。"""
    host = dict(info.get("HostConfig") or {})
    binds = list(host.get("Binds") or [])
    script = os.environ.get("SELF_UPDATE_SCRIPT") or os.path.abspath(__file__)

    payload = {
        "Image": info.get("Image"),  # 旧镜像：一定可用，即使新镜像是坏的
        "Cmd": [
            "python3", script, "--swap",
            "--old", old_id,
            "--new", new_id,
            "--name", base_name,
        ],
        "WorkingDir": "/app",
        "User": "0:0",
        "Env": [
            "DOCKER_SOCK=" + SOCK,
            "SELF_UPDATE_SCRIPT=" + script,
            "SELF_UPDATE_SWAP_DELAY=" + os.environ.get("SELF_UPDATE_SWAP_DELAY", "5"),
            "SELF_UPDATE_PRUNE=" + os.environ.get("SELF_UPDATE_PRUNE", "0"),
        ],
        "Labels": {"com.tarocats.wb2api.role": "selfupdate-swap"},
        "HostConfig": {
            "Binds": binds,
            # 用自己的网络命名空间：完全不碰宿主机端口，杜绝端口冲突
            "NetworkMode": "none",
            "AutoRemove": False,
            "RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3},
        },
    }
    swap_name = "%s-swap-%d" % (base_name, int(time.time()))
    if dry_run:
        log("dry-run：将用旧镜像 %s 创建调度容器 %s" % (short_id(info.get("Image")), swap_name))
        return None
    created = _post_json(d, "/containers/create?name=%s" % urllib.parse.quote(swap_name, safe=""), payload)
    swap_id = created["Id"]
    d.post("/containers/%s/start" % swap_id, ok=(204, 304))
    log("调度容器已启动：%s" % swap_id[:12])
    return swap_id


def _post_json(d, path, body=None, timeout=None, ok=(200, 201, 204)):
    data = d.post(path, body=body, timeout=timeout, ok=ok)
    if not data:
        return {}
    return json.loads(data.decode("utf-8", "replace"))


def run_once(image_ref, mode):
    d = Docker(SOCK)
    ver = d.connect()
    log("Docker %s / API %s" % (ver.get("Version"), ver.get("ApiVersion")))

    cid = self_container_id(d, os.environ.get("SELF_UPDATE_CONTAINER"))
    info = d.get_json("/containers/%s/json" % cid)
    base_name = (info.get("Name") or "").lstrip("/")
    cur_image_id = info.get("Image")
    image_name, tag = split_image_ref(image_ref)
    log("自身容器 %s（%s），当前镜像 %s，目标 %s" %
        (base_name, cid[:12], short_id(cur_image_id), image_ref))

    if mode != "check":
        cleanup_orphans(d, base_name, dry_run=(mode == "dry-run"))

    # 1) 便宜的先验：直接问 Registry 要 digest，不下载
    digest = remote_digest(d, image_name, tag)
    have = local_repo_digests(d, cur_image_id, image_name)
    if digest and digest in have:
        log("已是最新：远端与本地 digest 一致（%s）" % digest[:19])
        return EXIT_OK
    if mode == "check":
        if digest:
            log("有可用更新：远端 %s，本地 %s" % (digest, sorted(have) or "（未知）"))
            return EXIT_UPDATE_AVAILABLE
        log("无法判定是否有更新（distribution 接口不可用）")
        return EXIT_ERROR

    # 2) 拉取并比对镜像 ID（最终判据，不依赖 digest）
    log("拉取 %s ..." % image_ref)
    pull_image(d, image_name, tag)
    img = d.get_json("/images/%s/json" % image_ref)
    new_image_id = img.get("Id")
    if new_image_id == cur_image_id:
        log("拉取后镜像 ID 未变（%s），无需重建" % short_id(new_image_id))
        return EXIT_OK
    log("镜像已更新：%s → %s" % (short_id(cur_image_id), short_id(new_image_id)))

    if mode == "dry-run":
        log("dry-run：跳过创建/替换容器")
        return EXIT_UPDATE_AVAILABLE

    # 3) 用临时名创建新容器（不启动）。此时旧容器仍在正常服务，失败无影响。
    new_name = "%s-new-%d" % (base_name, int(time.time()))
    payload = build_create_payload(info, image_ref)
    created = _post_json(d, "/containers/create?name=%s" % urllib.parse.quote(new_name, safe=""),
                         payload, ok=(201,))
    new_id = created["Id"]
    log("新容器已创建（未启动）：%s（%s）" % (new_id[:12], new_name))

    # 4) 交给调度容器交割
    try:
        schedule_swap(d, info, cid, new_id, base_name)
    except Exception as e:
        log("调度容器创建/启动失败：%s" % e)
        log("回收刚创建的新容器，旧容器未受影响")
        try:
            d.delete("/containers/%s?force=1&v=1" % new_id)
        except DockerError:
            pass
        return EXIT_ERROR

    log("已安排自我替换：调度容器将在数秒内停止本容器、启动新容器。本容器日志到此为止。")
    return EXIT_OK


# ---------------------------------------------------------------- 入口

STOP = False


def _on_signal(signum, frame):
    global STOP
    STOP = True
    log("收到信号 %s，准备退出" % signum)


def loop(image_ref):
    interval = int(os.environ.get("SELF_UPDATE_INTERVAL", "21600"))
    jitter = int(os.environ.get("SELF_UPDATE_JITTER", "600"))
    log("自更新守护启动：每 %d 秒（+0~%d 秒抖动）检查一次" % (interval, jitter))
    while not STOP:
        try:
            run_once(image_ref, "once")
        except DockerError as e:
            log("本轮失败（不影响容器运行）：%s" % e)
        except Exception as e:  # 兜底：守护进程绝不能因为一次异常就死掉
            log("本轮异常（不影响容器运行）：%r" % e)

        wait = interval + (random.randint(0, jitter) if jitter > 0 else 0)
        log("下次检查在 %.2f 小时后" % (wait / 3600.0))
        deadline = time.time() + wait
        while not STOP and time.time() < deadline:
            time.sleep(max(0.0, min(5.0, deadline - time.time())))


def main(argv):
    args = argv[1:]
    mode = "once"
    old = new = final_name = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--check":
            mode = "check"
        elif a == "--once":
            mode = "once"
        elif a == "--dry-run":
            mode = "dry-run"
        elif a == "--loop":
            mode = "loop"
        elif a == "--swap":
            mode = "swap"
        elif a == "--old":
            i += 1
            old = args[i]
        elif a == "--new":
            i += 1
            new = args[i]
        elif a == "--name":
            i += 1
            final_name = args[i]
        elif a in ("-h", "--help"):
            print(__doc__)
            return EXIT_OK
        else:
            log("未知参数：%s" % a)
            print(__doc__)
            return EXIT_ERROR
        i += 1

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if mode == "swap":
        if not (old and new and final_name):
            log("--swap 需要 --old / --new / --name 三个参数")
            return EXIT_ERROR
        try:
            return do_swap(old, new, final_name)
        except DockerError as e:
            log("交割失败：%s" % e)
            return EXIT_ERROR

    image_ref = os.environ.get("SELF_UPDATE_IMAGE", DEFAULT_IMAGE)
    if mode == "loop":
        loop(image_ref)
        return EXIT_OK
    try:
        return run_once(image_ref, mode)
    except DockerError as e:
        log("失败：%s" % e)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv))
