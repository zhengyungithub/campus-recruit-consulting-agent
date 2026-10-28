# 项目长期记忆

## 仓库
- 远程：`https://github.com/zhengyungithub/campus-recruit-consulting-agent`（公开，默认分支 `main`）
- 本地分支叫 `master`，上游已设为 `origin/main`。
- 首次上传时 `github.com:443` 被网络策略拦截，先走了 GitHub Git Data API；
  随后用 SOCKS 跳板 `git -c http.proxy=socks5h://127.0.0.1:18080 push -f origin master:main` 覆盖，
  现在**远程 main 与本地 master 是同一个 Commit sha**，正常 `git push` 即可。

## 推送方式（本机必读）
```bash
ssh -i ~/.ssh/tencentcloud_ed25519 -D 127.0.0.1:18080 -N ubuntu@43.142.33.191 &   # 后台建隧道
git -c http.proxy=socks5h://127.0.0.1:18080 push origin master:main
```
直连或走系统代理都会失败（`CONNECT tunnel failed, response 502` / 超时）；
`git ls-remote` 经隧道偶发超时不影响 push。

## 生产部署（2026-10-28）
- 服务器：腾讯云 `ubuntu@43.142.33.191`（密钥 `~/.ssh/tencentcloud_ed25519`，必须显式 `-i`）。
- 代码 `/opt/campus-agent/src`，venv 同目录；前端 dist `/var/www/campus-agent`；
  后端 systemd `campus-agent` 监听 `127.0.0.1:8010`（**单 worker**，SSE 流式依赖它）。
- 环境文件 `/opt/campus-agent/env`（600，systemd EnvironmentFile）；更新用 `sudo /opt/campus-agent/deploy.sh`。
- **正式地址：`https://campus.zhengyun.online`**（2026-10-28 上线）。子域名就是 **`campus`**。
- DNS A 记录由用户在**阿里云控制台**手动添加（本机无阿里云 AK，代操作不了）。
- HTTPS 用 certbot webroot（校验目录 `/var/www/certbot`，与其他站点共用），自动续期已开启，证书每到 90 天续。
- nginx 站点 `/etc/nginx/sites-available/campus-agent.conf`：80 只留 ACME 通道并 301 跳 HTTPS，443 反代 8010。
  **nginx 是 1.24.0，必须写 `listen 443 ssl http2;`，不能用 `http2 on;`（要 1.25.1+）**。
- 同机还跑着另一个同栈应用 `/opt/hr-ai`（占 8000）和主站（占 3000），**别抢端口**。
- **改管理员密码**：生产 DB 的 `accounts` 表在首次启动时已固化当时的默认口令，改环境变量无效，
  必须走 `POST /api/admin/account`（旧口令校验）或后台页面。当前口令在 `/opt/campus-agent/env`，**从未进过仓库**。
- **口令解析唯一入口**：`agent/config.py::resolve_admin_password()`——环境变量 > `app/.env` > 随机生成并落盘。
  任何新代码**禁止写死口令默认值**（仓库公开，见避坑手册第 39 条）。
- **Git 历史已重写过**（2026-10-28）：用 `git-filter-repo` 清掉了历史中的明文口令。
  后果：远程 main 与本地是**同一份重写后的历史**（`ee3fef2` 起），服务器那份是 `git reset --hard origin/main` 对齐的。
  日常 `git pull` 正常；若再有人 `filter-repo`，本地/服务器都要重新对齐。

## 三端登录与角色模型（用户已确认的设计约定）
- 候选人/学生：`https://campus.zhengyun.online/`（redirect 到 `/c`），手机号+验证码；设过密码后可用手机号+密码。
- 后台：管理员与 HR **共用 `/admin` 同一个登录框**，靠 `/api/admin/login` 返回的 `role` 分流菜单。
- **super（超管）= 纯技术配置**（Agent 灰度/模型配置/HR 账号管理/评测回归/操作日志/主动推送）；
  **hr = 业务运营**（数据看板/候选人/知识库/反馈工单/主动推送）。二者菜单是**刻意互斥**的，
  **不要**因为"超管看不到候选人"就去补 super 的白名单——2026-10-28 用户明确回复这是设计意图。
- HR 账号不能自助注册，只能由超管通过 `POST /api/admin/accounts` 创建（role 默认 hr）。
- ⚠️ **任何账号口令都不要写进仓库文件**（含 `.workbuddy/memory/`）——仓库公开，口令只放在
  `/opt/campus-agent/env` 或当面给用户。

## 相关但独立的另一个仓库
- `zhengyungithub/onboarding-ai-assistant`：同一个项目的**另一条开发线**（无共同祖先），
  含"知途"品牌化登录页、两级角色+用户管理、系统级模型兜底、备忘录排序、nginx 部署等 12 个提交，
  不含本地 S4.4~S6.1 的工作。需要那些功能时去那边取，别当成同一个仓库的旧版本。
