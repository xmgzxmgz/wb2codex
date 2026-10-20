#!/usr/bin/env python3
"""
wb2codex_project.py — 把 WorkBuddy 转换出的 rollout 投影进 Codex 的 paginated 历史库

Codex 0.142 已移除 external_migration（官方 /import 开关），只能自己投影。
一套会话跨 3 处，缺任何一处都会出现 "no rollout found" 或空白对话：
  1. ~/.codex/state_5.sqlite          -> threads
  2. ~/.codex/thread_history_1.sqlite -> thread_items / thread_turns / projection_state
  3. ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

已确认的格式约束（全部踩过坑）：
  - rollout session_meta 必须是合法枚举: source=cli / originator=Codex Desktop / cli_version=0.142.3
  - turn_id 必须为 external-import-turn-1
  - mcpToolCall 的 item_id 是 call_id，不是 item-N
  - item_json 用紧凑分隔符序列化

用法：
  python3 wb2codex_project.py <rollout文件> [--dry-run]
  python3 wb2codex_project.py --all <目录> [--no-tools] [--dry-run]
"""
import os
import sys
import json
import glob
import shutil
import sqlite3
import argparse
import datetime as dt

CODEX = os.path.expanduser("~/.codex")
STATE = os.path.join(CODEX, "state_5.sqlite")
HIST = os.path.join(CODEX, "thread_history_1.sqlite")
SESSIONS = os.path.join(CODEX, "sessions")
TURN_ID = "external-import-turn-1"
MAX_OUTPUT = 20000


def detect_provider():
    cfg = os.path.join(CODEX, "config.toml")
    try:
        for line in open(cfg, encoding="utf-8"):
            s = line.strip()
            if s.startswith("model_provider") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "custom"


def _ms(ts):
    try:
        return int(dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    except Exception:
        return int(dt.datetime.now().timestamp() * 1000)


def build_items(rows, with_tools=True):
    """rollout 行 -> [(item_id, item_type, rollout_ordinal, created_at_ms, item_json)]"""
    result_map = {}
    for r in rows:
        pay = r.get("payload") or {}
        if pay.get("type") == "function_call_output":
            cid = pay.get("call_id")
            out = pay.get("output")
            if isinstance(out, dict):
                out = out.get("text") or json.dumps(out, ensure_ascii=False)
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            result_map[cid] = out

    items = []
    seq = 0
    for r in rows:
        pay = r.get("payload") or {}
        typ = pay.get("type")
        ordv = int(r.get("ordinal") or 0)
        ts = _ms(r.get("timestamp"))

        if typ == "message":
            role = pay.get("role")
            if role not in ("user", "assistant"):
                continue
            parts = [it["text"] for it in (pay.get("content") or [])
                     if isinstance(it, dict) and isinstance(it.get("text"), str)]
            text = "\n".join(parts).strip()
            if not text:
                continue
            seq += 1
            iid = f"item-{seq}"
            if role == "user":
                d = {"type": "userMessage", "id": iid, "clientId": None,
                     "content": [{"type": "text", "text": text, "text_elements": []}]}
                itype = "userMessage"
            else:
                d = {"type": "agentMessage", "id": iid, "text": text}
                itype = "agentMessage"
            items.append((iid, itype, ordv, ts,
                          json.dumps(d, ensure_ascii=False, separators=(",", ":"))))

        elif typ == "reasoning" and with_tools:
            parts = pay.get("content") or []
            txt = "\n".join(x.get("text", "") for x in parts if isinstance(x, dict)).strip()
            if not txt:
                continue
            seq += 1
            iid = f"item-{seq}"
            d = {"type": "reasoning", "id": iid, "summary": [], "content": []}
            items.append((iid, "reasoning", ordv, ts,
                          json.dumps(d, ensure_ascii=False, separators=(",", ":"))))

        elif typ == "function_call" and with_tools:
            cid = pay.get("call_id") or f"call_{seq}"
            args = pay.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"raw": args}
            elif not isinstance(args, dict):
                args = {"raw": str(args)}
            out = result_map.get(cid, "")
            if len(out) > MAX_OUTPUT:
                out = out[:MAX_OUTPUT] + "\n...(输出过长，已截断)"
            d = {"type": "mcpToolCall", "id": cid, "server": "workbuddy",
                 "tool": pay.get("name") or "unknown", "status": "completed",
                 "arguments": args, "appContext": None, "pluginId": None,
                 "readOnlyHint": None,
                 "result": {"content": [{"type": "text", "text": out}]},
                 "error": None, "durationMs": None}
            items.append((cid, "mcpToolCall", ordv, ts,
                          json.dumps(d, ensure_ascii=False, separators=(",", ":"))))
    return items


def sanitize_rows(rows):
    """只保留 Codex 投影器认的行：session_meta + message + event_msg。
    function_call / function_call_output / reasoning 会让它投影失败，必须剥掉。
    ordinal 重排为 0..N-1 连续（Codex 对 ordinal 做严格连续性校验）。"""
    out = []
    n = 0
    for r in rows:
        pay = r.get("payload") or {}
        t = r.get("type")
        keep = (t == "session_meta"
                or t == "event_msg"
                or (t == "response_item" and pay.get("type") == "message"
                    and pay.get("role") in ("user", "assistant")))
        if keep:
            r["ordinal"] = n
            n += 1
            out.append(r)
    return out


def project_one(path, with_tools=True, sanitize=False):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if sanitize:
        before = len(rows)
        rows = sanitize_rows(rows)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if before != len(rows):
            pass
    raw = open(path, "rb").read().splitlines(keepends=True)
    offs, acc = [], 0
    for l in raw:
        offs.append(acc)
        acc += len(l)
    total_bytes = acc

    meta = rows[0]["payload"]
    tid = meta["session_id"]
    cwd = meta.get("cwd") or os.path.expanduser("~")
    title = ""
    for r in rows[1:]:
        pay = r.get("payload") or {}
        if pay.get("type") == "message" and pay.get("role") == "user":
            c = pay.get("content") or []
            if c:
                title = (c[0].get("text") or "").strip().split("\n")[0][:60]
                break

    items = build_items(rows, with_tools=with_tools)
    if not items:
        return None

    first_user = next((x[0] for x in items if x[1] == "userMessage"), items[0][0])
    final_agent = next((x[0] for x in reversed(items) if x[1] == "agentMessage"), items[-1][0])
    first_ord = min(x[2] for x in items)
    last_ord = max(int(r.get("ordinal") or 0) for r in rows)
    turn_ord = 1 if first_ord >= 1 else 0
    turn_off = offs[turn_ord] if turn_ord < len(offs) else 0
    u = dt.datetime.strptime(rows[0]["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=dt.timezone.utc)
    created = int(u.timestamp())
    provider = detect_provider()

    dest_dir = os.path.join(SESSIONS, f"{u:%Y}", f"{u:%m}", f"{u:%d}")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if os.path.abspath(dest) != os.path.abspath(path):
        shutil.copyfile(path, dest)

    s = sqlite3.connect(STATE, timeout=30)
    s.execute("""INSERT OR REPLACE INTO threads
        (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
         sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
         first_user_message, preview, history_mode, model)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, dest, created, created, "cli", provider, cwd, title,
         '{"type":"disabled"}', "never", 0, 1, 0, "0.142.3",
         title, title, "paginated", None))
    s.commit()
    s.close()

    h = sqlite3.connect(HIST, timeout=30)
    h.execute("DELETE FROM thread_items WHERE thread_id=?", (tid,))
    h.execute("DELETE FROM thread_turns WHERE thread_id=?", (tid,))
    h.execute("DELETE FROM thread_history_projection_state WHERE thread_id=?", (tid,))
    for iid, itype, ordv, ms, ijson in items:
        h.execute("""INSERT INTO thread_items
            (thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_json,
             item_type, updated_at_ordinal, started_at_ms, completed_at_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, TURN_ID, iid, ordv, ms, ijson, itype, ordv, None, None))
    h.execute("""INSERT INTO thread_turns
        (thread_id, turn_id, rollout_ordinal, status, error_json, started_at, completed_at,
         duration_ms, first_user_item_id, final_agent_item_id, rollout_byte_offset,
         rollout_end_ordinal, rollout_end_byte_offset, root_turn_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, TURN_ID, turn_ord, "completed", None, None, created, None,
         first_user, final_agent, turn_off, last_ord, total_bytes, None))
    h.execute("""INSERT INTO thread_history_projection_state
        (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?,?,?)""",
        (tid, total_bytes, last_ord + 1))
    h.commit()
    h.close()
    return {"id": tid, "title": title, "rows": len(rows), "items": len(items),
            "user": sum(1 for x in items if x[1] == "userMessage"),
            "agent": sum(1 for x in items if x[1] == "agentMessage"),
            "tools": sum(1 for x in items if x[1] == "mcpToolCall")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="rollout 文件；配合 --all 时为目录")
    ap.add_argument("--all", action="store_true", help="批量处理目录下所有 rollout")
    ap.add_argument("--no-tools", action="store_true", help="不投影工具调用")
    ap.add_argument("--no-sanitize", action="store_true", help="不剥离工具调用行（会投影失败）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sanitize = not args.no_sanitize
    with_tools = (not args.no_tools) and not sanitize

    if args.all:
        files = sorted(glob.glob(os.path.join(os.path.expanduser(args.path), "*.jsonl")))
        if not files:
            print("目录下没有 jsonl", file=sys.stderr)
            return 1
        if not args.dry_run:
            stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copyfile(STATE, STATE + f".bak-projall-{stamp}")
            shutil.copyfile(HIST, HIST + f".bak-projall-{stamp}")
            print(f"已备份: *.bak-projall-{stamp}")
        ok = skipped = 0
        total_items = total_tools = 0
        for p in files:
            try:
                r = project_one(p, with_tools=with_tools, sanitize=sanitize)
            except Exception as e:
                skipped += 1
                print(f"  跳过 {os.path.basename(p)[:40]}: {e}", file=sys.stderr)
                continue
            if not r:
                skipped += 1
                continue
            ok += 1
            total_items += r["items"]
            total_tools += r["tools"]
            if ok % 50 == 0:
                print(f"  ...已处理 {ok}/{len(files)}")
        print(f"\n完成: {ok} 个会话, {skipped} 个跳过")
        print(f"  写入 {total_items} 条 item（其中工具调用 {total_tools} 条）")
        return 0

    p = os.path.expanduser(args.path)
    if not os.path.exists(p):
        print(f"找不到 {p}", file=sys.stderr)
        return 1
    if args.dry_run:
        raw = open(p, "rb").read().splitlines(keepends=True)
        rows = [json.loads(l) for l in raw]
        items = build_items(rows, with_tools=with_tools)
        print(f"[dry-run] {len(rows)} 行 -> {len(items)} 条 item "
              f"(user={sum(1 for x in items if x[1]=='userMessage')}, "
              f"agent={sum(1 for x in items if x[1]=='agentMessage')}, "
              f"tool={sum(1 for x in items if x[1]=='mcpToolCall')})")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copyfile(STATE, STATE + f".bak-proj-{stamp}")
    shutil.copyfile(HIST, HIST + f".bak-proj-{stamp}")
    r = project_one(p, with_tools=with_tools, sanitize=sanitize)
    if not r:
        print("没有可投影的内容", file=sys.stderr)
        return 1
    print(f"已投影 {r['id'][:8]} 「{r['title']}」")
    print(f"  {r['rows']} 行 -> {r['items']} 条 item "
          f"(user={r['user']}, agent={r['agent']}, tool={r['tools']})")
    print(f"备份: *.bak-proj-{stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
