#!/usr/bin/env python3
"""
labmeet — the async lab meeting between you and your autoresearch agent.

The meat computer still has opinions.
It reports results, you decide what to try next.
Over Telegram, from your bed, while experiments keep running.

Usage:
    export TELEGRAM_BOT_TOKEN="your-token"
    python3 labmeet.py [--repo /path/to/autoresearch]

Companion to: https://github.com/karpathy/autoresearch
"""

import argparse
import csv
import json
import logging
import os
import re
import signal
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("labmeet")

# ─── Configuration ───────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_MSG = 4096
POLL_INTERVAL = 30

# Metric columns where lower is better (losses). Everything else: higher is better.
LOWER_IS_BETTER = {"val_bpb", "bpb", "val_loss", "loss", "best_val_bpb", "error", "mse", "mae"}

def detect_metric(headers: list[str]) -> tuple[str, bool]:
    """Auto-detect the primary metric column and whether higher is better."""
    skip = {"timestamp", "params_hash", "notes", "hash", "commit", "description",
            "change", "kept", "status", "num_weeks"}
    for col in headers:
        if col.lower() in skip:
            continue
        hib = col.lower() not in LOWER_IS_BETTER
        return col, hib
    return headers[1] if len(headers) > 1 else "score", True


# ─── Git helpers ─────────────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()

def get_current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")

def get_latest_commit(repo: Path) -> dict:
    fmt = "%H%n%h%n%s%n%ai%n%an"
    out = git(repo, "log", "-1", f"--format={fmt}")
    if not out:
        return {}
    lines = out.split("\n")
    return {"hash": lines[0], "short_hash": lines[1], "subject": lines[2],
            "date": lines[3], "author": lines[4]}

def get_commit_diff(repo: Path, commit_hash: str) -> str:
    diff = git(repo, "diff", f"{commit_hash}~1", commit_hash, "--", "train.py")
    if not diff:
        diff = git(repo, "diff", f"{commit_hash}~1", commit_hash)
    return diff

def get_commit_count(repo: Path) -> int:
    out = git(repo, "rev-list", "--count", "HEAD")
    try:
        return int(out)
    except ValueError:
        return 0

def get_branch_commits(repo: Path, n: int = 20) -> list[dict]:
    fmt = "%h|%s|%ai"
    out = git(repo, "log", f"-{n}", f"--format={fmt}")
    if not out:
        return []
    commits = []
    for line in out.split("\n"):
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"short_hash": parts[0], "subject": parts[1], "date": parts[2]})
    return commits


# ─── Results parsing ─────────────────────────────────────────────────────────

def parse_results_tsv(repo: Path) -> tuple[list[dict], str, bool]:
    """Parse results.tsv. Returns (rows, metric_name, higher_is_better)."""
    tsv_path = repo / "results.tsv"
    if not tsv_path.exists():
        return [], "score", True
    try:
        with open(tsv_path) as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)
            headers = reader.fieldnames or []
        metric, hib = detect_metric(headers)
        return rows, metric, hib
    except Exception as e:
        log.warning(f"Failed to parse results.tsv: {e}")
        return [], "score", True

def parse_run_log(repo: Path) -> dict:
    """Extract metrics from run.log."""
    log_path = repo / "run.log"
    if not log_path.exists():
        return {}
    try:
        text = log_path.read_text()
        metrics = {}
        for pat in [r"val_bpb:\s*([\d.]+)", r"score:\s*([\d.]+)", r"best.*?:\s*([\d.]+)"]:
            m = re.search(pat, text)
            if m:
                metrics["run_metric"] = float(m.group(1))
                break
        m = re.search(r"peak_vram_mb:\s*([\d.]+)", text)
        if m:
            metrics["peak_vram_mb"] = float(m.group(1))
        if "Traceback" in text:
            metrics["error"] = "\n".join(text.strip().split("\n")[-10:])
        return metrics
    except Exception:
        return {}

def get_best_metric(results: list[dict], metric: str, higher_is_better: bool) -> float | None:
    """Find the best metric value from results."""
    best = None
    for r in results:
        try:
            val = float(r.get(metric, ""))
            if best is None:
                best = val
            elif higher_is_better and val > best:
                best = val
            elif not higher_is_better and val < best:
                best = val
        except (ValueError, TypeError):
            continue
    return best


# ─── Experiment state tracking ───────────────────────────────────────────────

class ExperimentTracker:
    def __init__(self, repo: Path, metric_name: str, higher_is_better: bool):
        self.repo = repo
        self.metric_name = metric_name
        self.higher_is_better = higher_is_better
        self.last_commit_hash: str | None = None
        self.experiment_count = 0
        self.improvements = 0
        self.best_score: float | None = None
        self.start_score: float | None = None
        self.start_time = datetime.now()
        self.history: list[dict] = []

    def is_better(self, new: float, old: float) -> bool:
        return new > old if self.higher_is_better else new < old

    def check_for_new_commit(self) -> dict | None:
        commit = get_latest_commit(self.repo)
        if not commit:
            return None
        if commit["hash"] == self.last_commit_hash:
            return None
        self.last_commit_hash = commit["hash"]
        return commit

    def record_experiment(self, commit: dict, score: float | None, improved: bool):
        self.experiment_count += 1
        if improved:
            self.improvements += 1
        if score is not None:
            if self.start_score is None:
                self.start_score = score
            if self.best_score is None or self.is_better(score, self.best_score):
                self.best_score = score
        self.history.append({"score": score, "commit": commit.get("short_hash", "?"),
                             "subject": commit.get("subject", ""), "improved": improved,
                             "timestamp": datetime.now().isoformat()})

    def format_progress_summary(self) -> str:
        elapsed = datetime.now() - self.start_time
        hours = elapsed.total_seconds() / 3600
        branch = get_current_branch(self.repo)
        direction = "↑ higher is better" if self.higher_is_better else "↓ lower is better"
        lines = ["📊 *Lab Meeting Summary*"]
        lines.append(f"Branch: `{branch}`")
        lines.append(f"Metric: `{self.metric_name}` ({direction})")
        lines.append(f"Experiments: {self.experiment_count}")
        lines.append(f"Improvements kept: {self.improvements}")
        lines.append(f"Running for: {hours:.1f}h")
        if self.best_score is not None:
            lines.append(f"Best: *{self.best_score:.6f}*")
        if self.start_score is not None and self.best_score is not None:
            delta = abs(self.best_score - self.start_score)
            pct = (delta / abs(self.start_score)) * 100 if self.start_score != 0 else 0
            lines.append(f"Improvement: {delta:.6f} ({pct:.2f}%)")
        if self.experiment_count > 0:
            rate = self.experiment_count / max(hours, 0.01)
            lines.append(f"Rate: ~{rate:.1f} experiments/hour")
        recent = [h for h in self.history[-20:] if h["score"] is not None]
        if len(recent) >= 3:
            vals = [h["score"] for h in recent]
            lines.append(f"Trend: {_sparkline(vals, self.higher_is_better)}")
        return "\n".join(lines)


def _sparkline(values: list[float], higher_is_better: bool = True) -> str:
    if not values:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    if mn == mx:
        return chars[4] * len(values)
    rng = mx - mn
    if higher_is_better:
        return "".join(chars[min(7, int((v - mn) / rng * 7))] for v in values)
    else:
        return "".join(chars[min(7, int((mx - v) / rng * 7))] for v in values)


# ─── Message formatting ─────────────────────────────────────────────────────

def format_new_experiment(commit: dict, metrics: dict, tracker: ExperimentTracker,
                          results: list[dict], metric_name: str) -> str:
    score = None
    if results:
        try:
            score = float(results[-1].get(metric_name, ""))
        except (ValueError, TypeError, IndexError):
            pass
    if score is None:
        score = metrics.get("run_metric")
    is_improvement = False
    if score is not None and tracker.best_score is not None:
        is_improvement = tracker.is_better(score, tracker.best_score)
    elif score is not None:
        is_improvement = True
    if "error" in metrics:
        status, label = "💥", "CRASHED"
    elif is_improvement:
        status, label = "🟢", "IMPROVED"
    else:
        status, label = "🔴", "REVERTED"
    lines = [f"{status} *Experiment #{tracker.experiment_count + 1}* — {label}"]
    if score is not None:
        lines.append(f"{metric_name}: `{score:.6f}`")
        if tracker.best_score is not None and score != tracker.best_score:
            delta = score - tracker.best_score
            arrow = "↑" if delta > 0 else "↓"
            lines.append(f"vs best: {arrow} {abs(delta):.6f}")
    lines.append(f"Commit: `{commit.get('short_hash', '?')}` {commit.get('subject', '')}")
    if "error" in metrics:
        lines.append(f"\n```\n{metrics['error'][:500]}\n```")
    if "peak_vram_mb" in metrics:
        lines.append(f"VRAM: {metrics['peak_vram_mb']:.0f} MB")
    tracker.record_experiment(commit, score, is_improvement)
    return "\n".join(lines)

def format_diff(repo: Path, commit_hash: str) -> str:
    diff = get_commit_diff(repo, commit_hash)
    if not diff:
        return "No diff available."
    if len(diff) > 3000:
        diff = diff[:1500] + "\n\n⋯ truncated ⋯\n\n" + diff[-1000:]
    return f"```diff\n{diff}\n```"

def format_results_table(results: list[dict], metric: str, last_n: int = 10) -> str:
    if not results:
        return "No results recorded yet."
    recent = results[-last_n:]
    lines = [f"Recent Results (last {len(recent)}):\n"]
    for r in recent:
        val = r.get(metric, "?")
        desc = r.get("notes", r.get("description", r.get("change", "")))[:60]
        desc = desc.replace("_", " ").replace("*", "").replace("`", "").replace("[", "(").replace("]", ")")
        lines.append(f"  {val} — {desc}")
    return "\n".join(lines)
    return "\n".join(lines)


# ─── program.md management ───────────────────────────────────────────────────

def read_program_md(repo: Path) -> str:
    path = repo / "program.md"
    return path.read_text() if path.exists() else "(program.md not found)"

def append_to_program_md(repo: Path, text: str) -> bool:
    path = repo / "program.md"
    if not path.exists():
        return False
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(path, "a") as f:
            f.write(f"\n\n<!-- Human direction via labmeet at {ts} -->\n{text}\n")
        return True
    except Exception as e:
        log.error(f"Failed to update program.md: {e}")
        return False

def write_note(repo: Path, text: str):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(repo / "human_notes.md", "a") as f:
            f.write(f"\n## [{ts}]\n{text}\n")
    except Exception as e:
        log.error(f"Failed to write note: {e}")


# ─── Telegram bot ────────────────────────────────────────────────────────────

def main():
    global CHAT_ID
    parser = argparse.ArgumentParser(description="labmeet — async lab meeting for autoresearch")
    parser.add_argument("--repo", type=str, default=os.getcwd(), help="Path to autoresearch repo")
    parser.add_argument("--poll", type=int, default=POLL_INTERVAL, help="Seconds between git checks")
    parser.add_argument("--metric", type=str, default=None, help="Override metric column name")
    parser.add_argument("--lower-is-better", action="store_true", help="Metric improves by going down")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    poll_interval = args.poll

    if not BOT_TOKEN:
        print("┌─────────────────────────────────────────┐\n"
              "│  labmeet setup                          │\n"
              "├─────────────────────────────────────────┤\n"
              "│  1. Message @BotFather on Telegram      │\n"
              "│  2. Send /newbot, follow the prompts    │\n"
              "│  3. Copy the token                      │\n"
              "│  4. export TELEGRAM_BOT_TOKEN='...'     │\n"
              "│  5. Re-run this script                  │\n"
              "└─────────────────────────────────────────┘")
        sys.exit(1)

    if not (repo / "program.md").exists():
        print(f"❌ {repo} doesn't look like an autoresearch repo.")
        print("   Expected program.md. Use --repo to specify path.")
        sys.exit(1)

    try:
        from telegram import Update
        from telegram.ext import (ApplicationBuilder, CommandHandler,
                                  ContextTypes, MessageHandler, filters)
    except ImportError:
        print("❌ pip install \"python-telegram-bot[job-queue]\"")
        sys.exit(1)

    # Auto-detect metric
    results, auto_metric, auto_hib = parse_results_tsv(repo)
    metric_name = args.metric or auto_metric
    higher_is_better = not args.lower_is_better if args.metric else auto_hib
    tracker = ExperimentTracker(repo, metric_name, higher_is_better)
    commit = get_latest_commit(repo)
    if commit:
        tracker.last_commit_hash = commit["hash"]
    if results:
        tracker.best_score = get_best_metric(results, metric_name, higher_is_better)
        tracker.experiment_count = len(results)
    branch = get_current_branch(repo)
    direction = "higher is better" if higher_is_better else "lower is better"
    log.info(f"labmeet monitoring: {repo}")
    log.info(f"Branch: {branch} | Metric: {metric_name} ({direction})")
    log.info(f"Existing experiments: {tracker.experiment_count}")
    if tracker.best_score is not None:
        log.info(f"Current best {metric_name}: {tracker.best_score}")

    # ── Handlers ──
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global CHAT_ID
        CHAT_ID = str(update.effective_chat.id)
        await update.message.reply_text(
            f"🔬 *labmeet is live*\n\nRepo: `{repo}`\nBranch: `{get_current_branch(repo)}`\n"
            f"Metric: `{metric_name}` ({direction})\nPolling every {poll_interval}s\n\n"
            f"I'll report when experiments complete. You steer.\n\n"
            f"/progress — stats & trend\n/results — recent results.tsv\n"
            f"/diff — last commit diff\n/program — show program.md\n"
            f"/steer <text> — append to program.md\n/note <text> — leave a note\n"
            f"/log — tail of run.log\n/branch — branch info", parse_mode="Markdown")

    async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(tracker.format_progress_summary(), parse_mode="Markdown")

    async def cmd_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
        r, _, _ = parse_results_tsv(repo)
        await update.message.reply_text(format_results_table(r, metric_name))

    async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
        c = get_latest_commit(repo)
        if not c: return await update.message.reply_text("No commits found.")
        d = format_diff(repo, c["hash"])
        if len(d) > MAX_MSG: d = d[:MAX_MSG - 20] + "\n```\n⋯"
        try: await update.message.reply_text(d, parse_mode="Markdown")
        except: await update.message.reply_text(d.replace("`", ""))

    async def cmd_program(update: Update, context: ContextTypes.DEFAULT_TYPE):
        c = read_program_md(repo)
        if len(c) > MAX_MSG - 20: c = c[:MAX_MSG - 40] + "\n\n⋯ (truncated)"
        await update.message.reply_text(f"```\n{c}\n```", parse_mode="Markdown")

    async def cmd_steer(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = " ".join(context.args) if context.args else ""
        if not text:
            return await update.message.reply_text("Usage: `/steer your direction here`", parse_mode="Markdown")
        if append_to_program_md(repo, text):
            await update.message.reply_text(
                f"✅ Appended to program.md:\n\n_{text}_\n\nAgent picks this up next iteration.",
                parse_mode="Markdown")
        else: await update.message.reply_text("❌ Failed to update program.md.")

    async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = " ".join(context.args) if context.args else ""
        if not text: return await update.message.reply_text("Usage: `/note your observation`", parse_mode="Markdown")
        write_note(repo, text)
        await update.message.reply_text("📝 Note saved.")

    async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
        lp = repo / "run.log"
        if not lp.exists(): return await update.message.reply_text("No run.log found.")
        try:
            tail = "\n".join(lp.read_text().strip().split("\n")[-30:])
            if len(tail) > MAX_MSG - 20: tail = tail[-(MAX_MSG - 30):]
            await update.message.reply_text(f"```\n{tail}\n```", parse_mode="Markdown")
        except: await update.message.reply_text("Failed to read run.log.")

    async def cmd_branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
        b = get_current_branch(repo)
        lines = [f"🌿 Branch: `{b}`", f"Commits: {get_commit_count(repo)}\n", "*Recent:*"]
        for c in get_branch_commits(repo, 5):
            lines.append(f"  `{c['short_hash']}` {c['subject']}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global CHAT_ID
        if not CHAT_ID: CHAT_ID = str(update.effective_chat.id)
        if str(update.effective_chat.id) != CHAT_ID:
            return await update.message.reply_text("⛔ Unauthorized.")
        text = update.message.text.strip()
        if not text: return
        write_note(repo, text)
        await update.message.reply_text(
            "📝 Noted. `/steer` to inject into program.md, `/progress` for status.", parse_mode="Markdown")

    async def watch_experiments(context: ContextTypes.DEFAULT_TYPE):
        if not CHAT_ID: return
        commit = tracker.check_for_new_commit()
        if not commit: return
        metrics = parse_run_log(repo)
        r, _, _ = parse_results_tsv(repo)
        msg = format_new_experiment(commit, metrics, tracker, r, metric_name)
        try: await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        except:
            try: await context.bot.send_message(chat_id=CHAT_ID,
                    text=msg.replace("*","").replace("`","").replace("\\",""))
            except: log.error("Failed to send message")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("program", cmd_program))
    app.add_handler(CommandHandler("steer", cmd_steer))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("branch", cmd_branch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(watch_experiments, interval=poll_interval, first=5)
    log.info(f"🔬 labmeet is running | {repo} | {metric_name} ({direction})")
    log.info(f"   Send /start to your bot to register" if not CHAT_ID else f"   Chat ID: {CHAT_ID}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
