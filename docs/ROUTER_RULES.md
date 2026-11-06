# Router 派生规则（让看板能画出调度树）

适用：核心协调人派主线 router，任何 router 再派子 router，子 router 再派子子 router。
目的：每个 router 在 Orca 里有自己的标签栏，看板能自动画出"谁派了谁"。

## 规则 1：新 router 必须有自己的子 worktree

被另一个 router 启动的 router，要放进**父 router 所在 worktree 的子 worktree**。这样它有独立的标签栏，在侧栏里嵌在父 router 下面，看板也能靠这层父子关系认出它。

两种做法，二选一：

1. 要在父 router 的 run 里被监督（推荐，同时产生"派单关系"这条线索）：

   ```
   orca orchestration worker-start --worktree new-child --agent <agent> --spec-file <spec> --name <id>-router ...
   ```

2. 不需要父 run 监督，或目标是远程主机：

   ```
   orca worktree create --parent-worktree <父 router 所在 worktree 的选择器> --name <id>-router --agent <agent> ...
   ```

   选择器可以是 `id:<repo-id>::<path>`、`path:<path>`、`worktree:<id>` 或 `active`。

注意：

- `new-child` 对远程主机无效，远程上一律用 `worktree create --parent-worktree`。
- 不要为新 router 用 `orca terminal create`、`orca agent launch` 或 `terminal split`。它们只是在父 worktree 里多开一个标签，没有独立标签栏，也没有父子关系。
- worker 照旧：`new-top-level` 或 `new-child` 都行，不受本规则影响。
- 用之前先看一遍 `orca worktree create --help` 和 `orca orchestration worker-start --help`，参数以帮助为准。

## 规则 2：新 router 要能追溯到父亲

新 router 起来后：

- 自己 `run-create`，目标写成 `<父主线 id>/<子名字>：<要做什么>`，例如 `EVB/扫描：跑多核扫描`。看板用它兜底认主线。
- 用 `progress dispatch` 记一条派出事件。
- 如果它会长期存在，变成一条正式主线：在 `project.json` 的这条主线上加 `parent` 字段，值是父主线 id（或核心 id）。

## 规则 3：长期运行的 root / router 不要登记成自己 run 的 worker

root 或 router 是长期会话，不会"完成任务后汇报"。把它登记成自己那个 run 的 worker，会让 Orca 不停地报 "Worker stopped without reporting"，陷入循环。

如果已经发生：对那条派单执行 `worker-abandon`。这是破坏性操作，必须先得到用户同意。

## 看板怎么画调度树

快照任务（`snapshot.py`）每 5 分钟把 `router_tree` 写进 live.json。看板不再单独画一块调度树：主线泳道每行标"子 router N"，点进主线后子 router 画成卡片，可以一层层点下去（见 DESIGN.md §6.2）。

一个 router 的父亲按下面顺序找，先找到的算：

1. project.json 里该主线的 `parent`（边类型 `declared`）。
2. 派单关系：它的终端是别的 run 里的 worker，那个 run 的协调人就是父亲（`dispatch`）。
3. worktree 父子关系：它所在 worktree 的父 worktree 里有另一个 router 的终端（`worktree`）。
4. 默认：主线的父亲是所属核心的协调人（`default`）。

只有挂在本项目已声明 router 下面的 run 才会被收进来，别的项目的 run 会被忽略。

每个 router 的页面上有：名称和类型（核心协调人 / 主线 / 子 router）、在线状态、最后输出的真实时间，以及它派出的在跑执行者卡片。一个执行者挂在派出它的最深一层 router 下面；没有子 router 的就按主线挂，再不行按核心挂；都对不上的放进"未归属"。悬停和详情写出"派出者"和调度链，例如"核心一协调人 › EVB › EVB-子router"。

## 规则 4：执行和审查分开排

run 节点交付产物后，另排一个 review 节点（`progress plan --kind review --deps <run 节点> --expected-minutes <较短>`）；review 只读和判断，不重跑重活，需要重跑就写 FAIL/PARTIAL 并另排新的 run 节点。
