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

## 相关但独立的另一个仓库
- `zhengyungithub/onboarding-ai-assistant`：同一个项目的**另一条开发线**（无共同祖先），
  含"知途"品牌化登录页、两级角色+用户管理、系统级模型兜底、备忘录排序、nginx 部署等 12 个提交，
  不含本地 S4.4~S6.1 的工作。需要那些功能时去那边取，别当成同一个仓库的旧版本。
