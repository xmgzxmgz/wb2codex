#!/usr/bin/env python3
"""
wb2codex — 把 WorkBuddy(CodeBuddy) 本地会话转成 Codex CLI rollout 格式

WorkBuddy 源： ~/.workbuddy/projects/<cwd转义>/<sessionId>.jsonl
Codex  目标： ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl

用法：
  python3 wb2codex.py list [--project KW] [--limit N]
  python3 wb2codex.py export <sessionId前缀> [--out DIR] [--keep-reasoning] [--keep-reminders]
  python3 wb2codex.py export-all --out DIR [--since 2026-10-01] [--limit N]

默认只写到 --out 目录，不动 ~/.codex。确认没问题后再手动 --install。
"""
import os
import sys
import json
import glob
import re
import shutil
import argparse
import datetime as dt

WB_ROOT = os.path.expanduser("~/.workbuddy/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")

# 导入的会话必须指向 config.toml 里真实存在的 provider，
# 否则 Codex 恢复会话时报 "Model provider xxx not found" 并卡住
DEFAULT_PROVIDER = "custom"


def _detect_provider():
    """从 ~/.codex/config.toml 读默认 model_provider，读不到就回退 custom"""
    cfg = os.path.expanduser("~/.codex/config.toml")
    try:
        for line in open(cfg, encoding="utf-8"):
            s = line.strip()
            if s.startswith("model_provider") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return DEFAULT_PROVIDER

REMINDER_RE = re.compile(r"<system-reminder[\s\S]*?</system-reminder>", re.I)
UQ_RE = re.compile(r"</?user_query>", re.I)


def clean_text(s):
    """剥掉 harness 包装标签，留下用户真正说的话"""
    if not isinstance(s, str):
        return ""
    s = REMINDER_RE.sub("", s)
    s = UQ_RE.sub("", s)
    return s.strip()


def ms_to_iso(ms):
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
               f"{int(ms) % 1000:03d}Z"
    except Exception:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def iter_sessions():
    """遍历所有 WorkBuddy 会话文件。
    同一 sessionId 可能散落在多个项目目录（会话分叉），按行数取最完整的那份。"""
    best = {}
    dupes = {}
    for path in glob.glob(os.path.join(WB_ROOT, "*", "*.jsonl")):
        try:
            lines = [l for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
        except Exception:
            continue
        rows = []
        for l in lines:
            try:
                rows.append(json.loads(l))
            except Exception:
                pass
        if not rows:
            continue
        sid = rows[0].get("sessionId") or os.path.basename(path)[:-6]
        cwd = next((r.get("cwd") for r in rows if r.get("cwd")), "")
        ts = min((r.get("timestamp", 0) for r in rows if isinstance(r.get("timestamp"), (int, float))), default=0)
        title = ""
        for r in rows:
            if r.get("type") == "ai-title" and r.get("aiTitle"):
                title = r["aiTitle"]
                break
        user_msgs = [r for r in rows if r.get("role") == "user" and r.get("type") == "message"]
        if not title and user_msgs:
            txt = _extract_text(user_msgs[0].get("content"))
            title = clean_text(txt)[:60]
        model = ""
        for r in rows:
            pd = r.get("providerData") or {}
            if pd.get("model"):
                model = pd["model"]
                break
        sess = {
            "path": path,
            "sessionId": sid,
            "cwd": cwd,
            "timestamp": ts,
            "title": title,
            "model": model,
            "n_rows": len(rows),
            "n_user": len(user_msgs),
            "rows": rows,
        }
        k = sid
        if k not in best:
            best[k] = sess
        else:
            dupes[k] = dupes.get(k, 1) + 1
            if sess["n_rows"] > best[k]["n_rows"]:
                best[k] = sess

    for k, s in best.items():
        s["dupes"] = dupes.get(k, 0)
        yield s


def _extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, dict) and isinstance(it.get("text"), str):
                parts.append(it["text"])
            elif isinstance(it, str):
                parts.append(it)
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", "") or ""
    return ""


def _content_to_codex(content, role):
    """WorkBuddy content -> Codex content 数组"""
    out = []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return out
    for it in content:
        if isinstance(it, str):
            it = {"type": "text", "text": it}
        if not isinstance(it, dict):
            continue
        text = it.get("text")
        if not isinstance(text, str) or not text:
            continue
        # user 侧一律 input_text；assistant 侧 output_text
        t = "input_text" if role == "user" else "output_text"
        out.append({"type": t, "text": text})
    return out


def convert(sess, keep_reasoning=False, keep_reminders=False, provider=None):
    """返回 Codex rollout 行列表（dict）"""
    provider = provider or _detect_provider()
    rows = sess["rows"]
    sid = sess["sessionId"]
    out = []
    ordinal = 0

    def emit(ts, typ, payload):
        nonlocal ordinal
        out.append({
            "timestamp": ms_to_iso(ts) if isinstance(ts, (int, float)) else ts,
            "ordinal": ordinal,
            "type": typ,
            "payload": payload,
        })
        ordinal += 1

    # 1) session_meta
    first_ts = sess["timestamp"] or int(dt.datetime.now().timestamp() * 1000)
    emit(first_ts, "session_meta", {
        "session_id": sid,
        "id": sid,
        "timestamp": ms_to_iso(first_ts),
        "cwd": sess["cwd"] or os.path.expanduser("~"),
        "originator": "WorkBuddy",
        "cli_version": "wb2codex-1.0",
        "source": "workbuddy",
        "model_provider": provider,
        "base_instructions": {"text": "You are Codex, a coding agent. This session was imported from WorkBuddy."},
    })

    # 2) 事件流
    for r in rows:
        t = r.get("type")
        ts = r.get("timestamp") or first_ts

        if t == "message":
            role = r.get("role") or "user"
            content = _content_to_codex(r.get("content"), role)
            if not content:
                continue
            if not keep_reminders:
                for it in content:
                    it["text"] = clean_text(it["text"])
                content = [it for it in content if it["text"]]
            if not content:
                continue
            emit(ts, "response_item", {
                "type": "message",
                "id": r.get("id") or "",
                "role": role,
                "content": content,
            })

        elif t == "reasoning":
            if not keep_reasoning:
                continue
            text = _extract_text(r.get("content")) or r.get("rawContent") or ""
            if isinstance(text, str):
                text = text.strip()
            if not text:
                continue
            emit(ts, "response_item", {
                "type": "reasoning",
                "id": "rs_" + (r.get("id") or ""),
                "summary": [],
                "content": [{"type": "reasoning_text", "text": text}],
            })

        elif t == "function_call":
            args = r.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            emit(ts, "response_item", {
                "type": "function_call",
                "id": "fc_" + (r.get("id") or ""),
                "name": r.get("name") or "",
                "arguments": args,
                "call_id": r.get("callId") or "",
            })

        elif t == "function_call_result":
            output = r.get("output")
            if isinstance(output, dict):
                output = output.get("text") or json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            emit(ts, "response_item", {
                "type": "function_call_output",
                "call_id": r.get("callId") or "",
                "output": output,
            })

    # 原会话中断时会有 function_call 没有返回结果，补一条终止记录，
    # 否则 Codex 回放时 Responses 序列不闭合
    have = {l["payload"]["call_id"] for l in out
            if l["payload"].get("type") == "function_call_output"}
    if any(l["payload"].get("type") == "function_call" and l["payload"]["call_id"] not in have
           for l in out):
        filled = []
        for l in out:
            filled.append(l)
            p = l["payload"]
            if p.get("type") == "function_call" and p["call_id"] not in have:
                filled.append({
                    "timestamp": l["timestamp"],
                    "ordinal": 0,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": p["call_id"],
                        "output": "（该工具调用在原会话中未返回结果，会话已中断）",
                    },
                })
        out = filled

    for i, l in enumerate(out):
        l["ordinal"] = i

    return out


def write_rollout(lines, out_dir, sess):
    os.makedirs(out_dir, exist_ok=True)
    first = lines[0]["timestamp"]
    stamp = first.replace(":", "-").replace(".", "-")[:19]
    name = f"rollout-{stamp}-{sess['sessionId']}.jsonl"
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    return path


def cmd_list(args):
    sessions = sorted(iter_sessions(), key=lambda s: s["timestamp"], reverse=True)
    if args.project:
        kw = args.project.lower()
        sessions = [s for s in sessions if kw in s["cwd"].lower() or kw in s["title"].lower()]
    if args.limit:
        sessions = sessions[:args.limit]
    print(f"{'sessionId':38s} {'时间':17s} {'消息':>4s}  {'模型':20s} 标题")
    print("-" * 140)
    for s in sessions:
        d = dt.datetime.fromtimestamp(s["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M") if s["timestamp"] else "?"
        print(f"{s['sessionId']:38s} {d:17s} {s['n_user']:>4d}  {s['model'][:20]:20s} {s['title'][:50]}")
    print(f"\n共 {len(sessions)} 个会话")


def _find(prefix, sessions):
    hits = [s for s in sessions if s["sessionId"].startswith(prefix)]
    return hits


def cmd_export(args):
    sessions = list(iter_sessions())
    hits = _find(args.session_id, sessions)
    if not hits:
        print(f"没找到 sessionId 前缀为 {args.session_id} 的会话", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"前缀匹配到 {len(hits)} 个会话，请给更长的前缀：", file=sys.stderr)
        for s in hits:
            print(f"  {s['sessionId']}  {s['title'][:50]}", file=sys.stderr)
        return 1
    s = hits[0]
    if getattr(args, "cwd", None):
        s["cwd"] = os.path.expanduser(args.cwd)
    if not os.path.isdir(s["cwd"] or ""):
        print(f"⚠️  原工作目录已不存在：{s['cwd']}", file=sys.stderr)
        print("   Codex resume 时会切不进去，建议加 --cwd <存在的目录>", file=sys.stderr)
    lines = convert(s, keep_reasoning=args.keep_reasoning, keep_reminders=args.keep_reminders, provider=_detect_provider())
    if not lines:
        print("转换后没有任何消息，可能是空会话", file=sys.stderr)
        return 1
    path = write_rollout(lines, args.out, s)
    print(f"已转换：{path}")
    print(f"  sessionId : {s['sessionId']}")
    print(f"  cwd       : {s['cwd']}")
    print(f"  原始行数  : {s['n_rows']}  ->  rollout 行数: {len(lines)}")
    print(f"\n导入 Codex：运行 `python3 {sys.argv[0]} install {path}`，或手动复制到")
    print(f"  {CODEX_SESSIONS}/<YYYY>/<MM>/<DD>/")
    return 0


def cmd_export_all(args):
    sessions = sorted(iter_sessions(), key=lambda s: s["timestamp"])
    if args.since:
        cut = dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp() * 1000
        sessions = [s for s in sessions if s["timestamp"] >= cut]
    if args.limit:
        sessions = sessions[-args.limit:]
    ok = skipped = 0
    for s in sessions:
        lines = convert(s, keep_reasoning=args.keep_reasoning, keep_reminders=args.keep_reminders, provider=_detect_provider())
        if not lines:
            skipped += 1
            continue
        write_rollout(lines, args.out, s)
        ok += 1
    print(f"转换完成：{ok} 个成功，{skipped} 个跳过 -> {args.out}")
    return 0


def cmd_install(args):
    src = args.rollout_path
    first = json.loads(open(src, encoding="utf-8").readline())
    stamp = first["timestamp"]
    d = dt.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    dest_dir = os.path.join(CODEX_SESSIONS, f"{d:%Y}", f"{d:%m}", f"{d:%d}")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest) and not args.force:
        print(f"目标已存在：{dest}（加 --force 覆盖）", file=sys.stderr)
        return 1
    with open(src, encoding="utf-8") as f, open(dest, "w", encoding="utf-8") as g:
        g.write(f.read())
    print(f"已放入：{dest}")
    print("重启 Codex 后运行 `codex resume` 应该能看到这条会话")
    return 0


def cmd_install_all(args):
    fs = sorted(glob.glob(os.path.join(args.dir, "*.jsonl")))
    if not fs:
        print(f"{args.dir} 下没有 rollout 文件", file=sys.stderr)
        return 1
    copied = skipped = 0
    failed = []
    for p in fs:
        try:
            with open(p, encoding="utf-8") as f:
                first = json.loads(f.readline())
            stamp = first["timestamp"][:19]
            d = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
        except Exception as e:
            failed.append((os.path.basename(p), str(e)))
            continue
        dest_dir = os.path.join(CODEX_SESSIONS, f"{d:%Y}", f"{d:%m}", f"{d:%d}")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dest) and not args.force:
            skipped += 1
            continue
        shutil.copyfile(p, dest)
        copied += 1
    print(f"安装完成：{copied} 个写入，{skipped} 个已存在跳过，{len(failed)} 个失败")
    for n, e in failed[:5]:
        print(f"  FAIL {n}: {e}", file=sys.stderr)
    print(f"目标目录：{CODEX_SESSIONS}")
    return 0


def cmd_reindex(args):
    """把已安装的 rollout 补进 ~/.codex/state_5.sqlite 的 threads 表。
    Codex 不会主动重扫历史目录，不补索引的话 resume 列表里看不到。"""
    import sqlite3
    db = os.path.join(os.path.expanduser("~/.codex"), "state_5.sqlite")
    if not os.path.exists(db):
        print(f"找不到 {db}", file=sys.stderr)
        return 1

    bak = db + ".bak-wb2codex-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copyfile(db, bak)
    print(f"已备份数据库：{bak}")

    fs = glob.glob(os.path.join(args.dir, "*.jsonl"))
    conn = sqlite3.connect(db, timeout=20)
    added = missing = 0
    for p in fs:
        try:
            rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
            meta = rows[0]["payload"]
            sid = meta["session_id"]
            u = dt.datetime.strptime(meta["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=dt.timezone.utc)
        except Exception:
            continue
        dest = os.path.join(CODEX_SESSIONS, f"{u:%Y}", f"{u:%m}", f"{u:%d}", os.path.basename(p))
        if not os.path.exists(dest):
            missing += 1
            continue
        first_user = ""
        for r in rows[1:]:
            pay = r.get("payload") or {}
            if pay.get("type") == "message" and pay.get("role") == "user":
                c = pay.get("content") or []
                if c and isinstance(c[0], dict):
                    first_user = (c[0].get("text") or "")[:400]
                    break
        title = first_user.split("\n")[0][:60]
        cur = conn.execute(
            """INSERT OR IGNORE INTO threads
               (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                first_user_message, preview, history_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, dest, int(u.timestamp()), int(u.timestamp()), "cli", _detect_provider(),
             meta.get("cwd", ""), title, '{"type":"disabled"}', "never", 0, 1, 0,
             "0.142.0", first_user, first_user, "paginated"))
        added += cur.rowcount
    conn.commit()
    total = conn.execute("select count(*) from threads").fetchone()[0]
    conn.close()
    print(f"新增索引 {added} 条；跳过（文件未安装）{missing} 个；threads 总数 {total}")
    return 0


def _iso(ms):
    if not isinstance(ms, (int, float)):
        return _iso_now()
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{int(ms) % 1000:03d}Z"


def _iso_now():
    n = dt.datetime.now(dt.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _uid():
    import uuid
    return str(uuid.uuid4())


def to_claude_lines(sess, include_reasoning=True):
    """WorkBuddy 原始行 -> Claude Code 会话行。
    Codex 官方 /import 只认 Claude Code / Cursor 格式，走这条路能由 Codex 自己完成投影。"""
    rows = sess["rows"]
    sid = sess["sessionId"]
    cwd = sess["cwd"] or os.path.expanduser("~")
    model = sess["model"] or "claude-sonnet-4-6"
    out = []
    prev = None

    def base(typ, uuid, ts):
        d = {
            "parentUuid": prev,
            "isSidechain": False,
            "type": typ,
            "uuid": uuid,
            "timestamp": _iso(ts),
            "sessionKind": "bg",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": cwd,
            "sessionId": sid,
            "version": "2.1.212",
            "gitBranch": "",
        }
        if typ == "user":
            d["promptId"] = uuid
            d["permissionMode"] = "default"
            d["origin"] = {"kind": "human"}
            d["promptSource"] = "typed"
        return d

    for r in rows:
        t = r.get("type")
        ts = r.get("timestamp")

        if t == "message" and (r.get("role") or "user") == "user":
            text = clean_text(_extract_text(r.get("content")))
            if not text:
                continue
            u = _uid()
            d = base("user", u, ts)
            d["message"] = {"role": "user", "content": text}
            out.append(d)
            prev = u

        elif t == "message" and r.get("role") == "assistant":
            text = _extract_text(r.get("content")).strip()
            if not text:
                continue
            u = _uid()
            d = base("assistant", u, ts)
            d["message"] = {
                "id": u,
                "type": "message",
                "role": "assistant",
                "model": model,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "content": [{"type": "text", "text": text}],
            }
            d["effort"] = "medium"
            d["session_id"] = sid
            out.append(d)
            prev = u

        elif t == "reasoning" and include_reasoning:
            txt = (_extract_text(r.get("content")) or r.get("rawContent") or "")
            if not isinstance(txt, str) or not txt.strip():
                continue
            u = _uid()
            d = base("assistant", u, ts)
            d["message"] = {
                "id": u,
                "type": "message",
                "role": "assistant",
                "model": model,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "content": [{"type": "thinking", "thinking": txt.strip(), "signature": ""}],
            }
            d["effort"] = "medium"
            d["session_id"] = sid
            out.append(d)
            prev = u

        elif t == "function_call":
            args = r.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"raw": args}
            elif not isinstance(args, dict):
                args = {"raw": str(args)}
            u = _uid()
            d = base("assistant", u, ts)
            d["message"] = {
                "id": u,
                "type": "message",
                "role": "assistant",
                "model": model,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "content": [{
                    "type": "tool_use",
                    "id": r.get("callId") or f"call_{u[:20]}",
                    "name": r.get("name") or "unknown",
                    "input": args,
                }],
            }
            d["effort"] = "medium"
            d["session_id"] = sid
            out.append(d)
            prev = u

        elif t == "function_call_result":
            output = r.get("output")
            if isinstance(output, dict):
                output = output.get("text") or json.dumps(output, ensure_ascii=False)
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            u = _uid()
            d = base("user", u, ts)
            d["promptSource"] = "tool"
            d["message"] = {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": r.get("callId") or "",
                    "content": output,
                }],
            }
            out.append(d)
            prev = u

    if sess.get("title"):
        out.append({"type": "ai-title", "aiTitle": sess["title"], "sessionId": sid})
        last_user = next((_extract_text(x["message"].get("content")) for x in reversed(out)
                          if x.get("type") == "user" and isinstance(x.get("message"), dict)
                          and isinstance(x["message"].get("content"), str)), "")
        out.append({"type": "last-prompt", "lastPrompt": last_user or sess["title"],
                    "leafUuid": prev, "sessionId": sid})

    return out


def cmd_to_claude(args):
    """导出为 Claude Code 格式，供 Codex 官方 /import 使用"""
    days = args.days
    cut = (dt.datetime.now().timestamp() - days * 86400) * 1000 if days else 0
    sessions = [s for s in iter_sessions() if s["timestamp"] >= cut]
    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    if args.limit:
        sessions = sessions[:args.limit]

    out_dir = os.path.expanduser(args.dir)
    os.makedirs(out_dir, exist_ok=True)
    written = 0
    for s in sessions:
        lines = to_claude_lines(s, include_reasoning=not args.no_reasoning)
        if not lines:
            continue
        path = os.path.join(out_dir, f"{s['sessionId']}.jsonl")
        if os.path.exists(path) and not args.force:
            continue
        with open(path, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")
        written += 1

    print(f"已写入 {written} 个 Claude Code 会话 -> {out_dir}")
    print(f"（筛选范围：最近 {days} 天，共匹配 {len(sessions)} 个）")
    print()
    print("下一步：在 Codex CLI 里执行")
    print("  codex")
    print("  > /import")
    print("  选择 Claude Code -> 勾选这些会话 -> 导入")
    print()
    print("注意：导入完成后建议删掉这批文件，避免污染你自己的 Claude Code 会话列表：")
    print(f"  ls {out_dir} | wc -l")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 会话 -> Codex rollout 转换器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出 WorkBuddy 本地会话")
    p.add_argument("--project", help="按 cwd/标题关键词过滤")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export", help="导出单个会话")
    p.add_argument("session_id", help="sessionId 或其前缀")
    p.add_argument("--out", default="./codex-import")
    p.add_argument("--cwd", help="覆盖目标 cwd（原目录已删除时用）")
    p.add_argument("--keep-reasoning", action="store_true", help="保留 reasoning 行")
    p.add_argument("--keep-reminders", action="store_true", help="保留 system-reminder 块")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("export-all", help="批量导出")
    p.add_argument("--out", default="./codex-import")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--limit", type=int)
    p.add_argument("--keep-reasoning", action="store_true")
    p.add_argument("--keep-reminders", action="store_true")
    p.set_defaults(func=cmd_export_all)

    p = sub.add_parser("install", help="把转换好的 rollout 放进 ~/.codex/sessions")
    p.add_argument("rollout_path")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("install-all", help="批量放入某个目录下所有 rollout")
    p.add_argument("dir")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install_all)

    p = sub.add_parser("reindex", help="把已安装的 rollout 补进 Codex 的 threads 索引")
    p.add_argument("dir", nargs="?", default="./codex-import")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("to-claude", help="导出 Claude Code 格式，供 Codex 官方 /import 使用")
    p.add_argument("--days", type=int, default=30, help="只要最近 N 天（Codex /import 限制 30 天 50 条）")
    p.add_argument("--limit", type=int, default=50)
    _home = os.path.expanduser("~")
    _enc = _home.replace("\\", "-").replace("/", "-").replace(":", "")
    p.add_argument("--dir", default=os.path.join(_home, ".claude", "projects", _enc))
    p.add_argument("--no-reasoning", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_to_claude)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
