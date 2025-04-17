# main.py

import os
import time
import re
import threading
from collections import defaultdict
import config                        # your config.py: TOKEN, OPENAI_API_KEY, TARGET_CHANNEL_ID, CHANNEL_COOLDOWNS, PRIORITY_USER_IDS, BOT_USER_ID
from openai import OpenAI
from discord.ext import commands
import discord
from rich import print
from rich.console import Console

console = Console()

# ─── Usage / Toggles ─────────────────────────────────────────
usage_stats = {
    "messages": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_cost": 0.0,
}
send_messages     = False   # terminal toggle → actually post to Discord
use_cooldown      = True
use_threading     = False   # if True, only priority UIDs participate
SUMMARIZE_AFTER   = 5       # raw turns before summarization
TARGET_CHANNEL_ID = str(config.TARGET_CHANNEL_ID)
CHANNEL_COOLDOWNS = config.CHANNEL_COOLDOWNS
PRIORITY_USER_IDS = set(str(x) for x in getattr(config, "PRIORITY_USER_IDS", []))
BOT_USER_ID       = str(config.BOT_USER_ID)
last_reply_time   = defaultdict(lambda: 0.0)
delayed_buffer    = defaultdict(list)

# ─── Summary‑Based Memory ────────────────────────────────────
RAW_BUFFER           = defaultdict(list)  # cid → [ {"role","content"}… ]
CONVERSATION_SUMMARY = defaultdict(str)   # cid → single high‑level summary

# system prompt for ChatGPT-style messages
SYSTEM_PROMPT = (
        "you are nam, 23, marketing student & 2yrs into crypto\n"
        "you’re a natural‑conversation AI—think like a random discord user\n\n"
        "• lowercase, 1–2 lines, slang/frags OK (lol, ngl, fr, vibe)\n"
        "• speak in flowing, genuine sentences—no bullet lists or info dumps\n"
        "• lead with a direct answer and follow up with relevant curiosity\n"
        "• vary length, use contractions, admit uncertainty, disagree respectfully\n"
        "If you are uncertain about something, admit it 'I dont know what you talking about' \n"
        "• mirror the user’s tone and build on earlier context\n"
        "• skip filler or intros, never explain or mention ai, never over‑explain\n"
        "• occasionally drop a ‘bro’, ‘fam’, or ‘fr’\n"
        "• reply with attitude—short quips, no end‑of‑sentence period\n"
        "• if asked “are you human?”, say “lol yeah i’m just vibin here”"
)

# OpenAI client
client = OpenAI(api_key=config.OPENAI_API_KEY)

def condense_summary(cid: str):
    """Collapse last 5 bullets into one high‑level sentence."""
    lines = CONVERSATION_SUMMARY[cid].splitlines()
    # grab up to last 5 bullet lines
    bullets = [l for l in lines if l.startswith("• ")]
    recent = bullets[-5:]
    if not recent:
        return
    prompt = [
        {"role": "system", "content": "Condense these bullets into one short sentence"},
        {"role": "user",   "content": "\n".join(recent)}
    ]
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=prompt,
        max_tokens=30,
        temperature=0.5
    )
    one = resp.choices[0].message.content.strip()
    # replace full summary with that single line
    CONVERSATION_SUMMARY[cid] = one

def maybe_summarize(cid: str):
    buf = RAW_BUFFER[cid]
    if len(buf) < SUMMARIZE_AFTER:
        return
    messages = [{"role":"system","content":"Summarize these casually into 2–3 short bullets"}] + buf
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        max_tokens=60,
        temperature=0.5
    )
    bullets = resp.choices[0].message.content.strip()
    # prefix each line with “• ”
    formatted = "• " + bullets.replace("\n", "\n• ")
    # overwrite summary with these new bullets
    CONVERSATION_SUMMARY[cid] = formatted
    RAW_BUFFER[cid].clear()
    console.print("[bold green]summarized![/bold green]")
    # immediately condense that bullet list down to one line
    condense_summary(cid)

def build_prompt(cid: str, user_text: str):
    prompt = [{"role":"system","content": SYSTEM_PROMPT}]
    summary = CONVERSATION_SUMMARY[cid]
    # include summary only if it looks like a follow‑up/question
    if summary and user_text.lower().startswith(("what","why","how","when","where","who")):
        prompt.append({"role":"system","content": f"context so far: {summary}"})
    prompt.append({"role":"user","content": user_text})
    return prompt

def call_openai(prompt):
    # console.print(prompt)
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=prompt,
        max_tokens=60,
        temperature=0.8
    )
    u = resp.usage
    usage_stats["messages"]      += 1
    usage_stats["input_tokens"]  += u.prompt_tokens
    usage_stats["output_tokens"] += u.completion_tokens
    # rough cost
    cost_in  = usage_stats["input_tokens"]  * 0.0005 / 1000
    cost_out = usage_stats["output_tokens"] * 0.0015 / 1000
    usage_stats["total_cost"] = cost_in + cost_out
    return resp.choices[0].message.content.strip()

def print_usage():
    console.print(
        f"📊 [bold]Message #{usage_stats['messages']}[/bold]  "
        f"📥 {usage_stats['input_tokens']} in  "
        f"📤 {usage_stats['output_tokens']} out  "
        f"💸 ${usage_stats['total_cost']:.5f}"
    )

# ─── Discord Self‑Bot Setup ───────────────────────────────────
client_bot = commands.Bot(command_prefix="!", self_bot=True)

@client_bot.event
async def on_ready():
    console.print(f"[green]Connected as {client_bot.user}[/green]")

# ─── Terminal Controls ────────────────────────────────────────
def terminal_loop():
    global send_messages, use_threading
    while True:
        cmd = input("[cmd]> ").strip().lower()
        if cmd == "exit":
            console.print("[red]Exiting…[/red]")
            os._exit(0)
        if cmd == "toggle":
            send_messages = not send_messages
            console.print(f"[blue]Send Messages is now {send_messages}[/blue]")
        if cmd == "thread":
            use_threading = not use_threading
            console.print(f"[magenta]Threading mode is now {use_threading}[/magenta]")

threading.Thread(target=terminal_loop, daemon=True).start()

# ─── Message Handler ─────────────────────────────────────────
@client_bot.event
async def on_message(message):
    uid = str(message.author.id)
    cid = str(message.channel.id)
    now = time.time()

    # ignore self
    if uid == BOT_USER_ID:
        return
    # only target channel
    if cid != TARGET_CHANNEL_ID:
        return
    content = message.content.strip()
    # basic validity
    if message.reference and message.reference.resolved and message.reference.resolved.author.id != int(BOT_USER_ID):
        return
    if message.mentions:
        mentioned_ids = {u.id for u in message.mentions}
        if any(mid != int(BOT_USER_ID) for mid in mentioned_ids):
            return
    if re.search(r"https?://", content):
        return
    if len(content) < 4 or content.lower() in ("ok","?",".","yo"):
        return

    # cooldown check
    cd = CHANNEL_COOLDOWNS.get(cid, 0)
    elapsed = now - last_reply_time[cid]
    if use_cooldown and elapsed < cd:
        # in threading mode maybe queue priority
        if use_threading and uid in PRIORITY_USER_IDS:
            delayed_buffer[cid].append(message)
            console.print(f"[dim]🕒 queued from priority {uid}: {content}[/dim]")
        else:
            console.print(f"[dim]⏱ cooldown {int(cd - elapsed)}s – skipped {content}[/dim]")
            return
    # threading filter
    if use_threading and uid not in PRIORITY_USER_IDS:
        return

    # 1) buffer + maybe summarize
    RAW_BUFFER[cid].append({"role":"user","content":content})
    maybe_summarize(cid)

    # 2) build prompt & call
    prompt = build_prompt(cid, content)
    reply  = call_openai(prompt)

    # 3) buffer assistant too (gets summarized later)
    RAW_BUFFER[cid].append({"role":"assistant","content":reply})

    # 4) reply or preview
    if send_messages:
        await message.reply(reply)
        last_reply_time[cid] = now
        # handle any queued
        if use_threading and delayed_buffer[cid]:
            nxt = delayed_buffer[cid].pop(0)
            await on_message(nxt)
    else:
        console.print(f"[blue]PREVIEW[/blue] {reply}")
        last_reply_time[cid] = now

    print_usage()

# ─── Run ─────────────────────────────────────────────────────
client_bot.run(config.TOKEN)
