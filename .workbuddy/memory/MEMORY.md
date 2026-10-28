# 项目长期记忆

## 仓库
- 远程：`https://github.com/zhengyungithub/campus-recruit-consulting-agent`（公开，默认分支 `main`）
- 本地分支叫 `master`，上游已设为 `origin/main`。
- **注意**：本项目远程历史是 2026-10-27 通过 GitHub Git Data API 上传的（当时 `github.com:443` 被网络策略拦截），
  内容是等价的，但**提交哈希与本地不一致**。在网络正常的环境执行一次 `git push -f origin master:main` 即可完全对齐；
  在此之前不要用 `git pull`（两条历史无共同祖先，会按 unrelated histories 合并）。

## 相关但独立的另一个仓库
- `zhengyungithub/onboarding-ai-assistant`：同一个项目的**另一条开发线**（无共同祖先），
  含"知途"品牌化登录页、两级角色+用户管理、系统级模型兜底、备忘录排序、nginx 部署等 12 个提交，
  不含本地 S4.4~S6.1 的工作。需要那些功能时去那边取，别当成同一个仓库的旧版本。
