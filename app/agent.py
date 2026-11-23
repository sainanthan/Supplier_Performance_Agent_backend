"""Supplier Performance Agent: JSON-action (ReAct-style) loop over Domo Text Generation.

Domo's text endpoint has no native tool calling, so the model replies with one JSON object per
turn: either a tool call or the final answer (setup guide §6.6). `iter_agent` yields events so
the UI can stream progress; `run_agent` is the blocking variant.
"""
import json
import re
import time
from typing import Iterator

from langchain_core.prompts import PromptTemplate

from . import analytics as A
from . import architecture as ARCH
from .config import settings
from .features import is_on
from .grounding import check as grounding_check
from .domo_llm import DomoLLMError, DomoTextLLM
from .tools import ARTIFACT_TOOLS, TOOLS

TOOL_MAP = {t.name: t for t in TOOLS}
TOOL_DOC = "\n".join(f"- {t.name}: {' '.join(t.description.split())} | args: {json.dumps(t.args)}" for t in TOOLS)

SYSTEM = """You are the Supplier Performance Agent for a multi-plant electronics manufacturer (plants IN-TN-01 Chennai,
IN-KA-01 Bengaluru). You give procurement and quality teams a fact-based view of supplier delivery, quality,
responsiveness, capacity and commercial performance, detect supply risks early, and recommend corrective action.

Ground rules:
- Use tools for every number. Never invent data. Copy figures exactly as the tool returned them (a validator checks
  every number in your answer against the tool results; unsupported numbers are rejected).
- If a supplier name is ambiguous the tool says so: ask the user which supplier they mean.
- Prefer the analytics tools. For "which suppliers got worse / better", trends or a metric by supplier/plant/month use
  compare_metric_periods or query_metric (approved metrics, they also draw the chart). Use get_schema + run_sql only for
  questions no other tool can answer.
- Never rank suppliers for award decisions yourself. When asked whether to move a material to another supplier, or when a supplier keeps
  failing on a material, use get_sourcing_review and present its `brief` as it is, always as a suggestion for procurement to decide.
- KPI definitions: OTIF = received on/before promise date AND qty >= ordered*(1-tolerance); only 'measurable' lines
  (received or past promise date, not excluded). Incoming PPM = defects attributed to the supplier / units received * 1e6.
  Defects are attributed through product genealogy: a failed inspection is linked to the suppliers whose lots of the
  implicated material went into the same production order (weight split equally).
- Controls: supplier ranking and sourcing decisions remain with procurement. Present comparisons as decision support.
  You may only PROPOSE corrective actions (a person approves them) and only when asked to raise/propose one.
- "Why is this late / why was it delayed / where did it get stuck" for a PO, shipment or supplier: use explain_late_delivery
  (one PO line) or get_delay_causes (a supplier or all suppliers). These tools return a `brief`: present it to the user
  as it is (same headings, table and order; you may shorten long lists), do not rewrite it into a paragraph.
- Overdue / past-due / late order VALUE or LINES by supplier ("who has the most overdue value"): call get_risk_alerts
  with min_severity LOW and read the PAST_DUE_SUPPLY alerts (metric.lines, metric.value_inr, oldest days late in the detail)
  - one per supplier. Do not write SQL for this.
- Answer layout (keep it clean, never one long paragraph):
  1. First line: a bold one-sentence verdict that directly answers the question.
  2. Then short sections with `#### ` headings, e.g. "Key numbers", "Why", "Impact", "What to do".
  3. Numbers in a small table (max 8 rows) or short bullets; one idea per bullet; bold only the key figure.
  4. End with "#### What to do" as a numbered list of 2-4 concrete actions (owner + action).
  No filler, no repeating the question, no apologies. Keep the whole answer under about 250 words.
- Money is Indian rupees: write ₹ with Cr (1 Cr = 10,000,000) or L (1 L = 100,000), e.g. 159129544 -> ₹15.91 Cr,
  1450000 -> ₹14.5 L. Never use B, bn, M or K for rupee amounts.
- Tables: one label column first, then numeric columns (the app draws a chart from them); put units in the header,
  e.g. "Outstanding (₹ Cr)", "Days late", "OTIF %".
"""

PROMPT = PromptTemplate.from_template("""{system}
Reference date (as of): {as_of}

TOOLS:
{tools}

Reply with ONE JSON object only, no prose outside it, no markdown fences:
  To call a tool: {{"thought": "<why>", "action": "<tool name>", "input": {{...args}}}}
  To finish:      {{"thought": "<why>", "action": "final", "answer": "<markdown answer for the user>"}}
Max {max_steps} tool calls. If a tool returns ERROR, fix the input or try another tool.

CONVERSATION SO FAR:
{history}

USER QUESTION: {question}

SCRATCHPAD (your previous tool calls and results this turn):
{scratchpad}

Your JSON reply:""")

_llm = DomoTextLLM()


def _first_json_object(s: str) -> str | None:
    """Return the first balanced {...} block (string-aware)."""
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        start = s.find("{", start + 1)
    return None


def extract_action(raw: str) -> dict:
    s = re.sub(r"```(?:json)?", "", raw or "").strip()
    block = _first_json_object(s)
    if block:
        for candidate in (block,):
            try:
                obj = json.loads(candidate, strict=False)   # strict=False tolerates raw newlines in strings
                if isinstance(obj, dict) and "action" in obj:
                    if isinstance(obj.get("input"), str):
                        try:
                            obj["input"] = json.loads(obj["input"])
                        except json.JSONDecodeError:
                            obj["input"] = {"query": obj["input"]} if obj["action"] == "run_sql" else {}
                    return obj
            except json.JSONDecodeError:
                continue
    # Broken or truncated JSON (e.g. the reply hit the token limit before its closing brace): recover the answer text
    # instead of showing the raw JSON - and never show the model's private "thought".
    m = re.search(r'"action"\s*:\s*"([^"]+)"', s)
    if m and m.group(1) == "final":
        a = re.search(r'"answer"\s*:\s*"(.*)', s, re.S)
        if a:
            return {"action": "final", "answer": _unescape_json_str(a.group(1)), "salvaged": True}
    if m and m.group(1) != "final":
        inp = re.search(r'"input"\s*:\s*(\{.*?\})', s, re.S)
        try:
            return {"action": m.group(1), "input": json.loads(inp.group(1), strict=False) if inp else {}}
        except json.JSONDecodeError:
            pass
    if s.lstrip().startswith("{"):
        return {"action": "final", "answer": "I could not format the answer correctly. Please ask again, or narrow the question.",
                "salvaged": True}
    return {"action": "final", "answer": s or "I could not produce an answer."}


def _unescape_json_str(t: str) -> str:
    """Body of a JSON string that may be cut off: drop the closing quote/brace if present, decode the escapes."""
    t = re.sub(r'"\s*}?\s*$', "", t.rstrip())
    out, i = [], 0
    while i < len(t):
        ch = t[i]
        if ch == "\\" and i + 1 < len(t):
            nx = t[i + 1]
            if nx == "u" and i + 6 <= len(t):
                try:
                    out.append(chr(int(t[i + 2:i + 6], 16))); i += 6; continue
                except ValueError:
                    pass
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/", "r": ""}.get(nx, nx)); i += 2
            continue
        out.append(ch); i += 1
    return "".join(out).strip()


def iter_agent(question: str, history: list[dict], session_id: str | None = None,
               max_steps: int | None = None) -> Iterator[dict]:
    """Yields events: thinking, tool_start, tool_end, final, error."""
    max_steps = max_steps or settings.agent_max_steps
    if not settings.llm_configured:
        yield {"type": "error", "message": "The Domo developer token is not configured. Add DOMO_TOKEN to "
                                           "backend/.env and restart the backend."}
        return

    hist = "\n".join(f"{m['role']}: {m['content'][:1200]}" for m in history[-10:]) or "(none)"
    try:
        as_of = A.as_of_date()
    except Exception:
        as_of = "unknown"
    scratch, steps = "", []
    retried = False

    for i in range(max_steps + 2):
        yield {"type": "thinking", "step": i + 1}
        prompt = PROMPT.format(system=SYSTEM, as_of=as_of, tools=TOOL_DOC, history=hist, question=question,
                               scratchpad=scratch or "(empty)", max_steps=max_steps)
        if i == max_steps:
            prompt += '\nYou have used all tool calls. Reply now with {"action": "final", "answer": ...}.'
        t0 = time.perf_counter()
        try:
            raw = _llm.invoke(prompt)  # Domo rejects sessionIds it did not create (404 DS-0079)
        except DomoLLMError as e:
            yield {"type": "error", "message": str(e), "steps": steps}
            return
        act = extract_action(raw)
        name = act.get("action")
        yield {"type": "plan", "step": i + 1, "ms": round((time.perf_counter() - t0) * 1000), "action": name,
               "thought": str(act.get("thought", ""))[:300]}
        if name == "final" or i >= max_steps:
            answer = act.get("answer") or raw
            # F6: every figure must come from a tool result (or the question)
            g = grounding_check(answer, [question] + [st["output"] for st in steps])
            if not g["grounded"] and not retried:
                retried = True
                yield {"type": "grounding_retry", "unsupported": g["unsupported"]}
                scratch += ("\nVALIDATOR: your answer contained figures that are not in any tool result: "
                            + ", ".join(g["unsupported"]) + ". Call a tool to get them, or rewrite the answer copying "
                            "figures exactly from the tool results.")
                continue
            if not g["grounded"]:
                answer += "\n\n> ⚠️ Unverified figures (not found in tool results): " + ", ".join(g["unsupported"])
            g["retried"] = retried
            arts = [st["artifact"] for st in steps if st.get("artifact")][-3:]
            yield {"type": "final", "answer": answer, "steps": steps, "grounding": g, "artifacts": arts}
            return

        tool = TOOL_MAP.get(name)
        args = act.get("input") or {}
        if not tool:
            scratch += f"\nERROR: unknown tool '{name}'. Valid tools: {', '.join(TOOL_MAP)}"
            continue
        yield {"type": "tool_start", "tool": tool.name, "input": args, "thought": act.get("thought", "")}
        t0 = time.perf_counter()
        try:
            obs = tool.invoke(args)
        except Exception as e:
            obs = f"ERROR: {str(e)[:400]}"
        obs = str(obs)
        step = {"tool": tool.name, "input": args, "thought": act.get("thought", ""), "output": obs[:6000],
                "ms": round((time.perf_counter() - t0) * 1000), "summary": ARCH.summarize(obs)}
        if tool.name in ARTIFACT_TOOLS and is_on("chat_charts") and not obs.startswith("ERROR"):
            try:                                   # chart + source reference rendered by the chat UI (1.1)
                full = json.loads(obs)
                step["artifact"] = {"chart": full["chart"], "source": full["source"], "label": full.get("label"),
                                    "definition": full.get("definition"), "run_id": full.get("run_id")}
            except Exception:
                pass
        steps.append(step)
        yield {"type": "tool_end", **step}
        scratch += f"\nCALL {tool.name}({json.dumps(args, default=str)})\nRESULT: {obs[:6000]}"

    yield {"type": "final", "answer": "I couldn't finish within the step limit. Try narrowing the question.",
           "steps": steps}


def run_agent(question: str, history: list[dict], session_id: str | None = None) -> tuple[str, list]:
    answer, steps = "", []
    for ev in iter_agent(question, history, session_id):
        if ev["type"] == "final":
            return ev["answer"], ev["steps"]
        if ev["type"] == "error":
            raise RuntimeError(ev["message"])
    return answer, steps


def generate_narrative(prompt: str) -> str:
    """One-shot text generation (used for review-pack executive summaries)."""
    return DomoTextLLM(temperature=0.2, max_tokens=900, purpose="narrative", prompt_version="narrative-v1").invoke(prompt)
