"""Parsing for Pico's legacy tagged model protocol."""

import json
import re


def parse_model_output(raw):
    text = str(raw).strip()
    json_tool = re.fullmatch(r"<tool>\s*(.*?)\s*</tool>", text, re.DOTALL)
    if json_tool:
        return _parse_json_tool(json_tool.group(1))
    if re.fullmatch(r"<tool(?:\s[^>]*)?>.*?</tool>", text, re.DOTALL):
        payload = parse_xml_tool(text)
        return ("tool", payload) if payload is not None else ("retry", retry_notice())
    final_match = re.fullmatch(r"<final>\s*(.*?)\s*</final>", text, re.DOTALL)
    if final_match:
        final = final_match.group(1).strip()
        return (
            ("final", final)
            if final
            else ("retry", retry_notice("model returned an empty <final> answer"))
        )
    problem = (
        "model returned an empty response"
        if not text
        else "model returned an incomplete or untyped protocol response"
    )
    return "retry", retry_notice(problem)


def _parse_json_tool(body):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "retry", retry_notice("model returned malformed tool JSON")
    if not isinstance(payload, dict):
        return "retry", retry_notice("tool payload must be a JSON object")
    if not str(payload.get("name", "")).strip():
        return "retry", retry_notice("tool payload is missing a tool name")
    args = payload.get("args", {})
    if args is None:
        payload["args"] = {}
    elif not isinstance(args, dict):
        return "retry", retry_notice()
    return "tool", payload


def retry_notice(problem=None):
    prefix = (
        f"Runtime notice: {problem}"
        if problem
        else ("Runtime notice: model returned malformed tool output")
    )
    return (
        f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
        'For multi-line files, prefer <tool name="write_file" path="file.py">'
        "<content>...</content></tool>."
    )


def parse_xml_tool(raw):
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.DOTALL)
    if not match:
        return None
    attrs = parse_attrs(match.group("attrs"))
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None
    body = match.group("body")
    args = dict(attrs)
    for key in (
        "content",
        "old_text",
        "new_text",
        "command",
        "task",
        "pattern",
        "path",
    ):
        if f"<{key}>" in body:
            args[key] = extract_raw(body, key)
    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}


def parse_attrs(text):
    attrs = {}
    pattern = r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
    for match in re.finditer(pattern, text):
        attrs[match.group(1)] = (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
    return attrs


def extract(text, tag):
    return _extract_tag(text, tag, strip=True)


def extract_raw(text, tag):
    return _extract_tag(text, tag, strip=False)


def _extract_tag(text, tag, strip):
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    result = text[start:] if end == -1 else text[start:end]
    return result.strip() if strip else result
